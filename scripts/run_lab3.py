"""Reproducible orchestration for Lab 3 checks and benchmark runs."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results" / "lab3_final"
BASE_URL = "http://localhost:8080"
PROM_URL = "http://localhost:9090"
R = 5


def write_json(relative: str, value: Any) -> None:
    destination = OUT / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def command(name: str, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    executable = args[0]
    if shutil.which(executable) is None and executable in {"kind", "helm"}:
        packages = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
        candidates = list(packages.glob(f"**/{executable}.exe")) if packages.exists() else []
        if candidates:
            args = [str(candidates[0]), *args[1:]]
    started = datetime.now(UTC).isoformat()
    result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True, encoding="utf-8",
                            errors="replace")
    record = {
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
        "command": args,
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    write_json(f"commands/{name}.json", record)
    if check and result.returncode:
        raise RuntimeError(f"Command failed ({name}): {result.stderr or result.stdout}")
    return result


def kubectl_json(args: list[str]) -> Any:
    result = subprocess.run(["kubectl", *args, "-o", "json"], cwd=ROOT, text=True,
                            capture_output=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    return json.loads(result.stdout)


def http(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        BASE_URL + path, data=body, method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        response = urllib.request.urlopen(request, timeout=15)
    except urllib.error.HTTPError as error:
        response = error
    response_body = response.read().decode("utf-8", errors="replace")
    return {
        "at": datetime.now(UTC).isoformat(),
        "method": method,
        "path": path,
        "request_body": payload,
        "status": response.status,
        "response_body": response_body,
        "response_headers": dict(response.headers.items()),
    }


def payload(index: int, *, status: str = "created") -> dict[str, Any]:
    return {
        "patient_code": f"LAB3-{index:04d}",
        "modality": ("CT", "MRI", "XR", "US", "PET")[index % 5],
        "priority": index % 5 + 1,
        "doctor": f"Doctor {index:04d}",
        "issued_at": "2026-10-25",
        "status": status,
    }


def wait_ready(expected: int, timeout: int = 300) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        deployment = kubectl_json(["-n", "lab3", "get", "deployment", "web"])
        status = deployment.get("status", {})
        last = {
            "replicas": status.get("replicas", 0),
            "ready": status.get("readyReplicas", 0),
            "available": status.get("availableReplicas", 0),
            "updated": status.get("updatedReplicas", 0),
        }
        if all(last[key] == expected for key in ("replicas", "ready", "available", "updated")):
            return last
        time.sleep(2)
    raise TimeoutError(f"web did not reach {expected} ready replicas: {last}")


def reset_fixed(replicas: int, name: str) -> None:
    command(f"{name}_delete_hpa", ["kubectl", "-n", "lab3", "delete", "hpa", "web",
                                     "--ignore-not-found"])
    command(f"{name}_scale", ["kubectl", "-n", "lab3", "scale", "deployment/web",
                              f"--replicas={replicas}"])
    command(f"{name}_restart", ["kubectl", "-n", "lab3", "rollout", "restart", "deployment/web"])
    command(f"{name}_rollout", ["kubectl", "-n", "lab3", "rollout", "status", "deployment/web",
                                "--timeout=300s"])
    write_json(f"stability/{name}.json", {"at": datetime.now(UTC).isoformat(),
                                           "state": wait_ready(replicas)})
    time.sleep(10)


def wait_hpa_one_for_60_seconds(name: str) -> None:
    stable_since = None
    samples = []
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        hpa = kubectl_json(["-n", "lab3", "get", "hpa", "web"])
        deployment = kubectl_json(["-n", "lab3", "get", "deployment", "web"])
        desired = hpa.get("status", {}).get("desiredReplicas")
        current = hpa.get("status", {}).get("currentReplicas")
        ready = deployment.get("status", {}).get("readyReplicas", 0)
        now = time.time()
        samples.append({"epoch": now, "desired": desired, "current": current, "ready": ready})
        if desired == current == ready == 1:
            stable_since = stable_since or time.monotonic()
            if time.monotonic() - stable_since >= 60:
                write_json(f"stability/{name}.json", {"passed": True, "samples": samples})
                return
        else:
            stable_since = None
        time.sleep(5)
    write_json(f"stability/{name}.json", {"passed": False, "samples": samples})
    raise TimeoutError("HPA did not remain at one replica for 60 seconds")


def reset_hpa(manifest: str, name: str) -> None:
    command(f"{name}_delete_hpa", ["kubectl", "-n", "lab3", "delete", "hpa", "web",
                                     "--ignore-not-found"])
    command(f"{name}_scale_one", ["kubectl", "-n", "lab3", "scale", "deployment/web",
                                   "--replicas=1"])
    command(f"{name}_restart", ["kubectl", "-n", "lab3", "rollout", "restart", "deployment/web"])
    command(f"{name}_rollout", ["kubectl", "-n", "lab3", "rollout", "status", "deployment/web",
                                "--timeout=300s"])
    wait_ready(1)
    command(f"{name}_apply_hpa", ["kubectl", "apply", "-f", str(ROOT / "k8s" / manifest)])
    wait_hpa_one_for_60_seconds(name)


PROM_QUERIES = {
    "web_requests": 'sum by(pod)(rate(http_requests_total{namespace="lab3",path="/referrals/{referral_id}",status=~"2.."}[20s]))',
    "server_p99": 'histogram_quantile(0.99,sum by(pod,le)(rate(http_request_duration_seconds_bucket{namespace="lab3",path="/referrals/{referral_id}"}[20s])))',
    "server_p99_all": 'histogram_quantile(0.99,sum by(le)(rate(http_request_duration_seconds_bucket{namespace="lab3",path="/referrals/{referral_id}"}[20s])))',
    "web_cpu_ratio": 'sum by(pod)(rate(container_cpu_usage_seconds_total{namespace="lab3",container="web",metrics_path="/metrics/cadvisor"}[20s]))/0.25',
    "cfs_throttling": 'sum by(pod)(rate(container_cpu_cfs_throttled_periods_total{namespace="lab3",container="web",metrics_path="/metrics/cadvisor"}[20s]))/sum by(pod)(rate(container_cpu_cfs_periods_total{namespace="lab3",container="web",metrics_path="/metrics/cadvisor"}[20s]))',
    "ready_replicas": 'kube_deployment_status_replicas_ready{namespace="lab3",deployment="web"}',
    "desired_replicas": 'kube_horizontalpodautoscaler_status_desired_replicas{namespace="lab3",horizontalpodautoscaler="web"}',
    "pod_nodes": 'kube_pod_info{namespace="lab3",pod=~"web-.*"}',
    "db_connections": 'pg_stat_database_numbackends{datname="referrals"}',
    "db_transactions": 'rate(pg_stat_database_xact_commit{datname="referrals"}[20s])+rate(pg_stat_database_xact_rollback{datname="referrals"}[20s])',
}


def prometheus_query_range(query: str, start: float, end: float) -> dict[str, Any]:
    params = urllib.parse.urlencode({"query": query, "start": start, "end": end, "step": 5})
    with urllib.request.urlopen(f"{PROM_URL}/api/v1/query_range?{params}", timeout=30) as response:
        return json.load(response)


def collect_metrics(name: str, summary: dict[str, Any]) -> None:
    start = float(summary["started_at_epoch"])
    end = start + float(summary["nominal_duration_seconds"]) + 10
    values = {key: prometheus_query_range(query, start, end)
              for key, query in PROM_QUERIES.items()}
    write_json(f"metrics/{name}.json", {
        "collected_at": datetime.now(UTC).isoformat(),
        "start": start,
        "end": end,
        "step_seconds": 5,
        "queries": PROM_QUERIES,
        "values": values,
    })


def run_generator(name: str, *, path: str = "/referrals/{id}",
                  profile_file: Path | None = None, seed: bool = True) -> dict[str, Any]:
    prefix = OUT / "raw" / name
    summary_file = prefix.with_suffix(".summary.json")
    if summary_file.exists():
        summary = json.loads(summary_file.read_text(encoding="utf-8"))
        if not (OUT / "metrics" / f"{name}.json").exists():
            collect_metrics(name, summary)
        return summary
    args = [sys.executable, "-B", str(ROOT / "scripts" / "lab3_generator.py"),
            "--base-url", BASE_URL, "--path", path, "--output", str(prefix)]
    if seed:
        args.extend(["--seed-file", str(OUT / "seed.json")])
    if profile_file is not None:
        args.extend(["--profile-file", str(profile_file)])
    result = command(f"generator_{name}", args)
    if result.stdout:
        print(result.stdout, flush=True)
    summary = json.loads(summary_file.read_text(encoding="utf-8"))
    collect_metrics(name, summary)
    return summary


def checks_and_seed() -> None:
    logs = []
    for method, path in (("GET", "/livez"), ("GET", "/healthz"), ("GET", "/metrics")):
        logs.append(http(method, path))

    created = http("POST", "/referrals", payload(9000))
    logs.append(created)
    referral_id = json.loads(created["response_body"])["id"]
    logs.append(http("GET", f"/referrals/{referral_id}"))
    logs.append(http("PUT", f"/referrals/{referral_id}", payload(9001, status="scheduled")))
    logs.append(http("DELETE", f"/referrals/{referral_id}"))
    logs.append(http("GET", f"/referrals/{referral_id}"))
    write_json("http/crud_check.json", logs)
    if [entry["status"] for entry in logs[-5:]] != [201, 200, 200, 204, 404]:
        raise AssertionError("CRUD cycle failed")

    persistence = []
    created = http("POST", "/referrals", payload(9100))
    persistence.append(created)
    persistent_id = json.loads(created["response_body"])["id"]
    persistence.append(http("GET", f"/referrals/{persistent_id}"))
    before = kubectl_json(["-n", "lab3", "get", "pod", "postgres-0"])
    command("persistence_delete_postgres", ["kubectl", "-n", "lab3", "delete", "pod", "postgres-0",
                                                "--wait=true", "--timeout=120s"])
    command("persistence_wait_postgres", ["kubectl", "-n", "lab3", "rollout", "status",
                                             "statefulset/postgres", "--timeout=300s"])
    time.sleep(5)
    after = kubectl_json(["-n", "lab3", "get", "pod", "postgres-0"])
    persistence.append(http("GET", f"/referrals/{persistent_id}"))
    write_json("http/persistence_check.json", {
        "before_uid": before["metadata"]["uid"],
        "after_uid": after["metadata"]["uid"],
        "different_pod_instance": before["metadata"]["uid"] != after["metadata"]["uid"],
        "requests": persistence,
    })
    if persistence[-1]["status"] != 200 or before["metadata"]["uid"] == after["metadata"]["uid"]:
        raise AssertionError("PVC persistence check failed")

    ids = [persistent_id]
    seed_log = []
    for index in range(99):
        entry = http("POST", "/referrals", payload(index))
        seed_log.append(entry)
        if entry["status"] != 201:
            raise AssertionError(f"Seed failed at {index}: {entry}")
        ids.append(json.loads(entry["response_body"])["id"])
    write_json("http/seed_requests.json", seed_log)
    write_json("seed.json", {"ids": ids, "count": len(ids)})

    checks = {
        "nodes": kubectl_json(["get", "nodes"]),
        "pods": kubectl_json(["get", "pods", "--all-namespaces"]),
        "web_deployment": kubectl_json(["-n", "lab3", "get", "deployment", "web"]),
        "postgres_statefulset": kubectl_json(["-n", "lab3", "get", "statefulset", "postgres"]),
        "pvc": kubectl_json(["-n", "lab3", "get", "pvc"]),
        "podmonitors": kubectl_json(["-n", "monitoring", "get", "podmonitors"]),
    }
    write_json("infra_objects.json", checks)
    command("kubectl_top", ["kubectl", "top", "pods", "-n", "lab3"])
    command("max_connections", ["kubectl", "-n", "lab3", "exec", "postgres-0", "--", "sh", "-ec",
                                 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "show max_connections"'])
    command("cluster_nodes", ["kubectl", "get", "nodes", "-o", "wide"])
    command("cluster_pods", ["kubectl", "get", "pods", "-A", "-o", "wide"])
    command("versions_kubectl", ["kubectl", "version", "-o", "json"])
    command("versions_kind", ["kind", "version"])
    command("versions_helm", ["helm", "version", "--short"])
    command("versions_docker", ["docker", "version"])


def stairs() -> dict[int, dict[str, Any]]:
    reset_fixed(5, "noop")
    run_generator("noop", path="/bench/noop", seed=False)
    results = {}
    for replicas in (1, 2, 3, 5):
        name = f"n{replicas}"
        reset_fixed(replicas, name)
        results[replicas] = run_generator(name)
    return results


def read_stair_summaries() -> dict[int, dict[str, Any]]:
    result = {}
    for replicas in (1, 2, 3, 5):
        path = OUT / "raw" / f"n{replicas}.summary.json"
        if not path.exists():
            raise FileNotFoundError(f"Run stairs first: {path}")
        result[replicas] = json.loads(path.read_text(encoding="utf-8"))
    return result


def profiles() -> None:
    stairs_data = read_stair_summaries()
    x = {n: max(summary["steps"], key=lambda row: row["x"])["x"]
         for n, summary in stairs_data.items()}
    low = .3 * x[1]
    spike = min(2.5 * x[1], .9 * x[5])
    short_profile = {"segments": [
        {"duration": 30, "rate": low},
        {"duration": 60, "rate": spike},
        {"duration": 90, "rate": low},
    ], "derived_from": {"X1": x[1], "X5": x[5], "spike": spike}}
    long_profile = {"segments": [
        {"duration": 30, "rate": low},
        {"duration": 60, "rate": spike},
        {"duration": 360, "rate": low},
    ], "derived_from": {"X1": x[1], "X5": x[5], "spike": spike}}
    write_json("profiles/short.json", short_profile)
    write_json("profiles/d5_long.json", long_profile)

    reset_fixed(5, "fixed_profile")
    run_generator("fixed_profile", profile_file=OUT / "profiles" / "short.json")

    reset_hpa("hpa-30.yaml", "hpa30_profile")
    run_generator("hpa30_profile", profile_file=OUT / "profiles" / "short.json")

    reset_hpa("hpa-30.yaml", "d5_hpa30")
    run_generator("d5_hpa30", profile_file=OUT / "profiles" / "d5_long.json")

    reset_hpa("hpa-300.yaml", "d5_hpa300")
    run_generator("d5_hpa300", profile_file=OUT / "profiles" / "d5_long.json")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("checks", "stairs", "profiles", "all"), default="all")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if args.phase in ("checks", "all"):
        checks_and_seed()
    if args.phase in ("stairs", "all"):
        if not (OUT / "seed.json").exists():
            raise FileNotFoundError("Run --phase checks before stairs")
        stairs()
    if args.phase in ("profiles", "all"):
        profiles()
    print(json.dumps({"finished": True, "phase": args.phase,
                      "at": datetime.now(UTC).isoformat()}, indent=2))


if __name__ == "__main__":
    main()
