"""Stop one of two web replicas during a 10-second GET stream."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN_NAME = os.getenv("LAB2_RUN_NAME", "lab2_final")
OUT = ROOT / "results" / RUN_NAME
PROJECT = os.getenv("LAB2_PROJECT", "vo-lab2-shutdown-check")
TEMPLATE = ROOT / "nginx" / "nginx.shutdown.conf"
COMPOSE = [
    "docker", "compose",
    "-f", "docker-compose.yml",
    "-f", "docker-compose.shutdown.yml",
    "-p", PROJECT,
]
COMPOSE_ENV = os.environ.copy()


def run(args):
    p = subprocess.run(args, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, check=True, env=COMPOSE_ENV)
    return p.stdout


def main():
    with tempfile.TemporaryDirectory(prefix=".lab2-shutdown-", dir=ROOT) as temp_dir:
        runtime_config = Path(temp_dir) / "nginx.conf"
        runtime_config.write_text(TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
        COMPOSE_ENV["SHUTDOWN_NGINX_CONF"] = runtime_config.as_posix()
        trace = []

        def logged(args):
            output = run(args)
            trace.append({"command": args, "output": output})
            return output

        logged(COMPOSE + ["up", "--build", "-d", "--scale", "web=2", "db", "web", "proxy"])
        nginx_config = logged(COMPOSE + ["exec", "-T", "proxy", "nginx", "-T"])
        retry_disabled = (
            "proxy_next_upstream off;" in nginx_config
            and "proxy_next_upstream_tries 1;" in nginx_config
        )
        if not retry_disabled:
            raise RuntimeError("Graceful-stop check requires proxy retries to be disabled")
        shutdown_seed = f"/results/{RUN_NAME}/shutdown_seed.json"
        seed_command = COMPOSE + [
            "--profile", "load", "run", "--rm", "--no-deps",
            "--entrypoint", "python", "load-generator",
            "/load/load_generator.py", "seed", "--count", "100",
            "--output", shutdown_seed,
        ]
        logged(seed_command)
        ids = logged(COMPOSE + ["ps", "-q", "web"]).splitlines()
        if len(ids) != 2:
            raise RuntimeError(f"Expected 2 web replicas, got {len(ids)}")
        live_ip = logged([
            "docker", "inspect", "--format",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", ids[0],
        ]).strip()
        if not live_ip:
            raise RuntimeError("Could not resolve the web replica kept in service")
        generator = COMPOSE + ["--profile", "load", "run", "--rm", "--no-deps",
                               "--entrypoint", "python", "load-generator", "/load/lab2_async_generator.py",
                               "open", "--path", "/referrals/{id}",
                               "--seed-file", shutdown_seed, "--rate", "100",
                               "--duration", "10", "--timeout", "5", "--sample-interval-ms", "100",
                               "--output", f"/results/{RUN_NAME}/raw/graceful_stop_r100.json"]
        started = time.time()
        process = subprocess.Popen(generator, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, env=COMPOSE_ENV)
        time.sleep(4)

        # Remove the target from the load balancer first.  Reloading nginx lets
        # old workers finish their current requests before the container gets
        # SIGTERM, while new requests use only the surviving replica.
        drained_config = runtime_config.read_text(encoding="utf-8").replace(
            "server web:8000;", f"server {live_ip}:8000;"
        )
        runtime_config.write_text(drained_config, encoding="utf-8")
        logged(COMPOSE + ["exec", "-T", "proxy", "nginx", "-t"])
        logged(COMPOSE + ["exec", "-T", "proxy", "nginx", "-s", "reload"])
        reloaded_config = logged(COMPOSE + ["exec", "-T", "proxy", "nginx", "-T"])
        upstream_drained = f"server {live_ip}:8000;" in reloaded_config
        if not upstream_drained:
            raise RuntimeError("The stopping replica was not removed from nginx upstream")
        time.sleep(2)
        stopped = logged(["docker", "stop", "--timeout", "10", ids[-1]])
        exit_code = logged(["docker", "inspect", "--format", "{{.State.ExitCode}}", ids[-1]]).strip()
        stdout, _ = process.communicate(timeout=40)
        trace.append({"command": generator, "output": stdout, "exit_code": process.returncode})
        raw = json.loads((OUT / "raw" / "graceful_stop_r100.json").read_text(encoding="utf-8"))
        (OUT / "shutdown_seed.json").unlink(missing_ok=True)

        # Exercise the generic exception path: an unavailable database makes
        # the repository call fail, and the API must still return X-Pod.
        logged(COMPOSE + ["stop", "db"])
        time.sleep(2)
        error_status = None
        error_headers = {}
        error_body = ""
        try:
            urllib.request.urlopen("http://127.0.0.1:8080/referrals/1", timeout=10)
        except urllib.error.HTTPError as exc:
            error_status = exc.code
            error_headers = dict(exc.headers.items())
            error_body = exc.read().decode("utf-8", errors="replace")
        finally:
            logged(COMPOSE + ["start", "db"])
        error_x_pod = next(
            (value for key, value in error_headers.items() if key.lower() == "x-pod"), None
        )
        result = {"started_epoch": started, "stopped_container": ids[-1],
                  "compose_project": PROJECT,
                  "proxy_retries_disabled": retry_disabled,
                  "load_balancer_drain": upstream_drained,
                  "surviving_upstream_ip": live_ip,
                  "docker_stop_output": stopped.strip(), "web_exit_code": exit_code,
                  "generator_exit_code": process.returncode,
                  "responses_2xx": raw["responses_2xx"],
                  "responses_non_2xx": raw["responses_non_2xx"],
                  "request_timeouts": raw["request_timeouts"],
                  "connection_errors": raw["connection_errors"],
                  "request_exceptions": raw["request_exceptions"],
                  "unhandled_error_check": {
                      "database_stopped": True,
                      "status": error_status,
                      "x_pod": error_x_pod,
                      "body": error_body,
                  },
                  "trace": trace}
        (OUT / "graceful_stop_r100.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps({k: v for k, v in result.items() if k != "trace"}, indent=2))
        if (process.returncode or exit_code != "0" or not retry_disabled
                or not upstream_drained or raw["responses_non_2xx"]
                or raw["request_exceptions"] or error_status != 500
                or not error_x_pod):
            raise RuntimeError("Graceful stop criterion failed; inspect graceful_stop_r100.json")


if __name__ == "__main__":
    main()
