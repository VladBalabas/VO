"""Run reproducible Lab 2 experiments in a dedicated Compose project.

Requires Docker Desktop and an existing .env. Never deletes a volume or raw result.
"""
from __future__ import annotations

import json
import math
import os
import platform
import re
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN_NAME = os.getenv("LAB2_RUN_NAME", "lab2_final")
if not re.fullmatch(r"lab2(?:_[a-z0-9-]{1,40})?", RUN_NAME):
    raise ValueError("LAB2_RUN_NAME must be lab2 or lab2_<lowercase-name>")
OUT = ROOT / "results" / RUN_NAME
PROJECT = "vo-lab2"
DOCKER = ["docker", "compose", "-p", PROJECT]


def save(name: str, value: object) -> None:
    path = OUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def command(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    print("$", " ".join(args), flush=True)
    result = subprocess.run(args, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, encoding="utf-8", errors="replace")
    if check and result.returncode:
        print(result.stdout[-4000:], flush=True)
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(args)}")
    return result


def prom(path: str, params: dict[str, object]) -> object:
    url = "http://localhost:9090/api/v1/" + path + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as response:
        data = json.load(response)
    if data["status"] != "success":
        raise RuntimeError(data)
    return data["data"]


QUERIES = {
    "targets": "up{job=\"web\"}",
    "server_p99": 'histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket{job="web",path="/referrals/{referral_id}"}[15s])))',
    "web_cpu": 'rate(process_cpu_seconds_total{job="web"}[15s])',
    "web_in_flight": 'http_requests_in_flight{job="web"}',
    "web_requests": 'sum by (instance) (rate(http_requests_total{job="web",path="/referrals/{referral_id}"}[15s]))',
    "db_connections": 'pg_stat_database_numbackends{job="postgres"}',
    "db_active": 'sum by (datname) (pg_stat_activity_count{job="postgres",state="active"})',
    "db_idle": 'sum by (datname) (pg_stat_activity_count{job="postgres",state="idle"})',
    "db_tps": 'rate(pg_stat_database_xact_commit{job="postgres"}[15s]) + rate(pg_stat_database_xact_rollback{job="postgres"}[15s])',
    "server_buckets": 'sum by (le) (rate(http_request_duration_seconds_bucket{job="web",path="/referrals/{referral_id}"}[15s]))',
}


def captures(name: str, started: float, ended: float) -> None:
    raw = {}
    for key, query in QUERIES.items():
        try:
            raw[key] = prom("query_range", {"query": query, "start": math.floor(started)-5,
                                            "end": math.ceil(ended)+15, "step": 5})
        except Exception as exc:
            raw[key] = {"error": str(exc)}
    save(f"prometheus/{name}.json", raw)


def stats_during(name: str, done: threading.Event) -> None:
    samples = []
    while not done.is_set():
        docker = command(["docker", "stats", "--no-stream", "--format",
                          "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.PIDs}}"], check=False)
        host = command(["powershell", "-NoProfile", "-Command",
                        "Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor -Filter \"Name='_Total'\" | Select-Object -ExpandProperty PercentProcessorTime"], check=False)
        samples.append({"epoch": time.time(), "docker_exit_code": docker.returncode,
                        "docker_output": docker.stdout, "host_cpu_exit_code": host.returncode,
                        "host_cpu_percent": host.stdout.strip()})
        done.wait(5)
    save(f"stats/{name}_profile.json", {"samples": samples})


def profile(name: str, path: str, seed: bool = False, g: float | None = None) -> dict[str, object]:
    target = f"/results/{RUN_NAME}/raw/{name}_profile.json"
    args = DOCKER + ["--profile", "load", "run", "--rm", "--no-deps", "--entrypoint", "python",
                     "load-generator", "/load/lab2_async_generator.py", "profile", "--path", path,
                     "--timeout", "5",
                     "--sample-interval-ms", "100", "--output", target]
    if seed:
        args += ["--seed-file", f"/results/{RUN_NAME}/seed.json"]
    if g is not None:
        args += ["--g-ceiling", str(g)]
    started = time.time()
    done = threading.Event()
    sampler = threading.Thread(target=stats_during, args=(name, done), daemon=True)
    sampler.start()
    try:
        result = command(args, check=False)
    finally:
        done.set()
        sampler.join(timeout=30)
    ended = time.time()
    save(f"logs/{name}.json", {"command": args, "started_epoch": started,
                              "ended_epoch": ended, "exit_code": result.returncode,
                              "stdout": result.stdout})
    if result.returncode:
        raise RuntimeError(f"Generator failed for {name}: {result.stdout[-2000:]}")
    raw = json.loads((OUT / "raw" / f"{name}_profile.json").read_text(encoding="utf-8"))
    if name == "noop" and not raw["stop_two_misses"]:
        raise RuntimeError("/noop did not reach the required two-miss stopping condition")
    if raw["mode"] != "profile":
        raise RuntimeError("Expected one continuous profile")
    captures(name, started, ended)
    for item in raw["steps"]:
        save(f"steps/{name}_{item['rate']}.json", item)
    print(name, "steps=", len(raw["steps"]), "max=", max(s["x"] for s in raw["steps"]), flush=True)
    return raw


def scale(n: int) -> None:
    # Every N is an independent run.  Recreating all web processes prevents
    # timed-out client requests from a saturated previous profile from
    # continuing to consume the old process and contaminating the next point.
    command(DOCKER + ["up", "-d", "--force-recreate", "--scale", f"web={n}", "web"])
    command(DOCKER + ["restart", "proxy"])
    for attempt in range(45):
        time.sleep(2)
        data = prom("query", {"query": QUERIES["targets"]})
        healthy = [x for x in data["result"] if x["value"][1] == "1"]
        if len(healthy) == n:
            save(f"targets/n{n}.json", data)
            return
    raise RuntimeError(f"Prometheus has {len(healthy)} healthy web targets, expected {n}")


def wait_for_drain(n: int, label: str) -> None:
    """Wait until only the Prometheus /metrics scrape is visible per replica.

    The middleware increments the gauge while /metrics itself is served, so an
    idle system normally reports one in-flight request per replica at scrape
    time. Three consecutive idle observations also protect the next profile
    from the previous profile's timed-out requests that are still executing on
    the server.
    """
    query = 'sum(http_requests_in_flight{job="web"})'
    samples: list[dict[str, object]] = []
    consecutive_idle = 0
    deadline = time.time() + 90
    while time.time() < deadline:
        data = prom("query", {"query": query})
        result = data.get("result", [])
        value = float(result[0]["value"][1]) if result else math.inf
        sample = {"epoch": time.time(), "in_flight": value,
                  "idle_threshold": n + 0.1}
        samples.append(sample)
        consecutive_idle = consecutive_idle + 1 if value <= n + 0.1 else 0
        if consecutive_idle >= 3:
            save(f"drain/{label}.json", {"replicas": n, "query": query,
                                         "status": "drained", "samples": samples})
            return
        time.sleep(5)
    save(f"drain/{label}.json", {"replicas": n, "query": query,
                                 "status": "timeout", "samples": samples})
    raise RuntimeError(f"Previous workload did not drain before {label}")


def main() -> None:
    resume = os.getenv("LAB2_RESUME") == "1" or "--resume" in sys.argv[1:]
    if OUT.exists() and not resume:
        raise RuntimeError(f"Refusing to overwrite existing raw results: {OUT}")
    if not OUT.exists():
        OUT.mkdir(parents=True)
        save("environment.json", {"utc": datetime.now(timezone.utc).isoformat(),
                                  "platform": platform.platform(), "python": sys.version,
                                  "docker_info": command(["docker", "info", "--format",
                                                          "{{.NCPU}} CPUs; {{.MemTotal}} bytes RAM"]).stdout,
                                  "web_cpu_limit": os.getenv("WEB_CPUS", "0.5"),
                                  "db_pool_size": os.getenv("DB_POOL_SIZE", "15")})
    command(DOCKER + ["up", "-d", "db", "web", "proxy", "prometheus", "postgres-exporter"])
    if not resume:
        scale(1)
        wait_for_drain(1, "before_seed")
        seed_result = command(DOCKER + ["--profile", "load", "run", "--rm", "--no-deps",
                                      "load-generator", "seed", "--count", "100", "--output",
                                      f"/results/{RUN_NAME}/seed.json"])
        save("logs/seed.json", {"command": "load-generator seed --count 100", "output": seed_result.stdout})
        noop = profile("noop", "/noop")
        g = max(float(s["x"]) for s in noop["steps"])
        save("generator_ceiling.json", {"G": g, "steps": noop["steps"],
                                       "stop_two_misses": noop["stop_two_misses"]})
        first_n = 1
    else:
        required = [OUT / "seed.json", OUT / "raw" / "noop_profile.json",
                    OUT / "generator_ceiling.json"]
        if not all(path.exists() for path in required):
            raise RuntimeError("Resume requires existing seed, noop raw and generator ceiling")
        g = float(json.loads((OUT / "generator_ceiling.json").read_text(encoding="utf-8"))["G"])
        first_n = next((n for n in range(1, 5)
                        if not (OUT / "series" / f"n{n}.json").exists()), 5)
    for n in range(first_n, 5):
        scale(n)
        wait_for_drain(n, f"before_n{n}")
        raw = profile(f"n{n}", "/referrals/{id}", seed=True, g=g)
        save(f"series/n{n}.json", {"n": n, "steps": raw["steps"],
                                  "generator_ceiling_G": g,
                                  "stop_two_misses": raw["stop_two_misses"]})
    print("Finished all N=1..4 runs", flush=True)


if __name__ == "__main__":
    main()
