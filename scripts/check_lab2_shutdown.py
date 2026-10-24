"""Stop one of two web replicas during a 10-second GET stream."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN_NAME = os.getenv("LAB2_RUN_NAME", "lab2_final")
OUT = ROOT / "results" / RUN_NAME
COMPOSE = ["docker", "compose", "-p", "vo-lab2"]


def run(args):
    p = subprocess.run(args, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, check=True)
    return p.stdout


def main():
    trace = []
    def logged(args):
        output = run(args)
        trace.append({"command": args, "output": output})
        return output

    logged(COMPOSE + ["up", "-d", "--scale", "web=2", "web"])
    logged(COMPOSE + ["restart", "proxy"])
    ids = logged(COMPOSE + ["ps", "-q", "web"]).splitlines()
    if len(ids) != 2:
        raise RuntimeError(f"Expected 2 web replicas, got {len(ids)}")
    generator = COMPOSE + ["--profile", "load", "run", "--rm", "--no-deps",
                           "--entrypoint", "python", "load-generator", "/load/lab2_async_generator.py",
                           "open", "--path", "/referrals/{id}",
                           "--seed-file", f"/results/{RUN_NAME}/seed.json", "--rate", "100",
                           "--duration", "10", "--timeout", "5", "--sample-interval-ms", "100",
                           "--output", f"/results/{RUN_NAME}/raw/graceful_stop_r100.json"]
    started = time.time()
    process = subprocess.Popen(generator, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
    time.sleep(6)
    stopped = logged(["docker", "stop", "--timeout", "10", ids[-1]])
    exit_code = logged(["docker", "inspect", "--format", "{{.State.ExitCode}}", ids[-1]]).strip()
    stdout, _ = process.communicate(timeout=40)
    trace.append({"command": generator, "output": stdout, "exit_code": process.returncode})
    raw = json.loads((OUT / "raw" / "graceful_stop_r100.json").read_text(encoding="utf-8"))
    result = {"started_epoch": started, "stopped_container": ids[-1],
              "docker_stop_output": stopped.strip(), "web_exit_code": exit_code,
              "generator_exit_code": process.returncode,
              "responses_2xx": raw["responses_2xx"],
              "responses_non_2xx": raw["responses_non_2xx"],
              "request_timeouts": raw["request_timeouts"],
              "connection_errors": raw["connection_errors"],
              "request_exceptions": raw["request_exceptions"], "trace": trace}
    (OUT / "graceful_stop_r100.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in result.items() if k!="trace"},indent=2))
    if process.returncode or exit_code != "0" or raw["responses_non_2xx"] or raw["request_exceptions"]:
        raise RuntimeError("Graceful stop criterion failed; inspect graceful_stop.json")


if __name__ == "__main__":
    main()
