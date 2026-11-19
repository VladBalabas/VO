"""Відтворюваний оркестратор експериментів лабораторної роботи №4."""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import re
import shutil
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results" / "lab4_final"
RUNS = OUT / "runs"
BASE_URL = "http://127.0.0.1:8080"
PROM_URL = "http://127.0.0.1:9090"
NAMESPACE = "lab4"
R = 5
GENERATOR_VERSION = "lab4-threaded-open-loop-v2"


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def command(name: str, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    executable = args[0]
    if shutil.which(executable) is None and executable in {"kind", "helm"}:
        packages = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
        candidates = list(packages.glob(f"**/{executable}.exe"))
        if candidates:
            args = [str(candidates[0]), *args[1:]]
    started = utcnow()
    result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True,
                            encoding="utf-8", errors="replace")
    write_json(OUT / "commands" / f"{name}.json", {
        "started_at": started, "finished_at": utcnow(), "command": args,
        "exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
    })
    if check and result.returncode:
        raise RuntimeError(f"Команда {name} завершилася помилкою: {result.stderr or result.stdout}")
    return result


def kubectl(name: str, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return command(name, ["kubectl", *args], check)


def psql(query: str) -> str:
    args = [
        "kubectl", "-n", NAMESPACE, "exec", "statefulset/postgres", "--",
        "sh", "-ec",
        'PGPASSWORD="$POSTGRES_PASSWORD" psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" '
        '-d "$POSTGRES_DB" -At -F "\t" -c "$1"',
        "sh", query,
    ]
    result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True,
                            encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    return result.stdout.strip()


def psql_json(query: str) -> Any:
    text = psql(f"SELECT row_to_json(q) FROM ({query}) AS q;")
    return json.loads(text) if text else None


def http(method: str, path: str, payload: dict[str, Any] | None = None,
         timeout: float = 15) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        BASE_URL + path, data=body, method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    started = time.perf_counter()
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error
    except Exception as error:
        return {"at": utcnow(), "method": method, "path": path, "request_body": payload,
                "status": None, "response_body": "", "response_headers": {},
                "duration_ms": (time.perf_counter() - started) * 1000,
                "error_type": type(error).__name__, "error": str(error)}
    response_body = response.read().decode("utf-8", errors="replace")
    return {"at": utcnow(), "method": method, "path": path, "request_body": payload,
            "status": response.status, "response_body": response_body,
            "response_headers": dict(response.headers.items()),
            "duration_ms": (time.perf_counter() - started) * 1000,
            "error_type": None, "error": None}


def referral_payload(index: int) -> dict[str, Any]:
    return {"patient_code": f"LAB4-{index:04d}",
            "modality": ("CT", "MRI", "XR", "US", "PET")[index % 5],
            "priority": index % 5 + 1, "doctor": f"Лікар {index:04d}",
            "issued_at": "2026-11-09", "status": "created"}


def prepare_referrals() -> list[int]:
    existing = http("GET", "/referrals?limit=100&offset=0")
    if existing["status"] == 200:
        items = json.loads(existing["response_body"])["items"]
        if len(items) >= 100:
            ids = [item["id"] for item in items[:100]]
            write_json(OUT / "seed.json", {"reused": True, "ids": ids})
            return ids
    records = []
    for index in range(100):
        record = http("POST", "/referrals", referral_payload(index))
        if record["status"] != 201:
            raise RuntimeError(f"Не вдалося створити направлення: {record}")
        records.append(record)
    ids = [json.loads(record["response_body"])["id"] for record in records]
    write_json(OUT / "seed.json", {"reused": False, "ids": ids, "requests": records})
    return ids


def reset_tasks() -> None:
    psql("TRUNCATE TABLE task_results, tasks RESTART IDENTITY;")


def set_result_uniqueness(enabled: bool) -> None:
    if enabled:
        psql("""
        DELETE FROM task_results a USING task_results b
        WHERE a.task_id=b.task_id AND a.ctid>b.ctid;
        DROP INDEX IF EXISTS idx_task_results_task_id;
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='task_results_pkey') THEN
            ALTER TABLE task_results ADD CONSTRAINT task_results_pkey PRIMARY KEY(task_id);
          END IF;
        END $$;
        """)
    else:
        psql("""
        ALTER TABLE task_results DROP CONSTRAINT IF EXISTS task_results_pkey;
        CREATE INDEX IF NOT EXISTS idx_task_results_task_id ON task_results(task_id);
        """)


def configure_workers(name: str, replicas: int, *, mode: str = "skip_locked",
                      poll_ms: int = 100, fail_rate: float = 0,
                      batch: int = 1, idempotent: bool = True) -> None:
    kubectl(f"{name}_scale_zero", ["-n", NAMESPACE, "scale", "deployment/worker", "--replicas=0"])
    patch = json.dumps({"data": {"CLAIM_MODE": mode, "POLL_INTERVAL_MS": str(poll_ms),
                                 "FAIL_RATE": str(fail_rate), "CLAIM_BATCH_SIZE": str(batch),
                                 "IDEMPOTENT_RESULTS": str(idempotent).lower()}})
    kubectl(f"{name}_config", ["-n", NAMESPACE, "patch", "configmap", "worker-config",
                                "--type=merge", "-p", patch])
    kubectl(f"{name}_restart", ["-n", NAMESPACE, "rollout", "restart", "deployment/worker"])
    kubectl(f"{name}_scale", ["-n", NAMESPACE, "scale", "deployment/worker",
                               f"--replicas={replicas}"])
    if replicas:
        kubectl(f"{name}_rollout", ["-n", NAMESPACE, "rollout", "status",
                                     "deployment/worker", "--timeout=300s"])


def stop_workers(name: str) -> None:
    kubectl(f"{name}_scale_zero", ["-n", NAMESPACE, "scale", "deployment/worker", "--replicas=0"])
    kubectl(f"{name}_wait_deleted", ["-n", NAMESPACE, "wait", "--for=delete",
                                      "pod", "-l", "app=worker", "--timeout=90s"])


def runtime_signal(name: str, pod: str, signal_name: str) -> None:
    payload = json.loads(subprocess.run(
        ["kubectl", "-n", NAMESPACE, "get", "pod", pod, "-o", "json"],
        cwd=ROOT, text=True, capture_output=True, encoding="utf-8", check=True,
    ).stdout)
    node = payload["spec"]["nodeName"]
    container_id = payload["status"]["containerStatuses"][0]["containerID"].removeprefix("containerd://")
    inspected = command(name + "_inspect", ["docker", "exec", node, "crictl", "inspect",
                                                "--output", "go-template", "--template",
                                                "{{.info.pid}}", container_id])
    runtime_pid = inspected.stdout.strip()
    command(name, ["docker", "exec", node, "kill", f"-{signal_name}", runtime_pid])


def send_task(referral_id: int, priority: int, seed: int | None,
              scheduled_at: float, origin: float, index: int) -> dict[str, Any]:
    started = time.perf_counter()
    payload = {"priority": priority}
    if seed is not None:
        payload["seed"] = seed
    response = http("POST", f"/referrals/{referral_id}/tasks", payload)
    finished = time.perf_counter()
    task_id = None
    if response["status"] == 202:
        task_id = json.loads(response["response_body"])["id"]
    return {"index": index, "scheduled_offset_ms": (scheduled_at - origin) * 1000,
            "started_offset_ms": (started - origin) * 1000,
            "completed_offset_ms": (finished - origin) * 1000,
            "schedule_lag_ms": max(0.0, (started - scheduled_at) * 1000),
            "latency_from_schedule_ms": (finished - scheduled_at) * 1000,
            "task_id": task_id, **response}


def write_request_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def generate_tasks(path: Path | None, referral_ids: list[int], count: int, rate: float,
                   priority: Callable[[int], int] | None = None,
                   seed: Callable[[int], int | None] | None = None, *,
                   origin: float | None = None, start_offset: float = 0.0,
                   index_offset: int = 0) -> list[dict[str, Any]]:
    origin = time.perf_counter() if origin is None else origin
    futures = []
    workers = min(64, max(8, math.ceil(rate)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        for local_index in range(count):
            index = index_offset + local_index
            scheduled = origin + start_offset + local_index / rate
            delay = scheduled - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            futures.append(executor.submit(
                send_task, referral_ids[index % len(referral_ids)],
                priority(index) if priority else 2,
                seed(index) if seed else None, scheduled, origin, index,
            ))
        rows = [future.result() for future in futures]
    rows.sort(key=lambda row: row["index"])
    if path is not None:
        write_request_rows(path, rows)
    return rows


def task_counts() -> dict[str, Any]:
    return psql_json("""
    SELECT clock_timestamp() AS db_at,
           COUNT(*) FILTER (WHERE status='queued')::int AS queued,
           COUNT(*) FILTER (WHERE status='running')::int AS running,
           COUNT(*) FILTER (WHERE status='done')::int AS done,
           COUNT(*) FILTER (WHERE status='failed')::int AS failed,
           COUNT(*)::int AS total
    FROM tasks
    """)


def sample_database(path: Path, stop: threading.Event, interval: float = 1.0) -> None:
    samples = []
    while not stop.is_set():
        started = time.perf_counter()
        try:
            sample = psql_json("""
            SELECT clock_timestamp() AS db_at,
              COUNT(*) FILTER (WHERE status='queued')::int AS queued,
              COUNT(*) FILTER (WHERE status='running')::int AS running,
              COUNT(*) FILTER (WHERE status='done')::int AS done,
              COUNT(*) FILTER (WHERE status='failed')::int AS failed,
              (SELECT COUNT(*)::int FROM pg_stat_activity WHERE wait_event_type='Lock') AS lock_waiters,
              (SELECT xact_commit+xact_rollback FROM pg_stat_database
               WHERE datname=current_database())::bigint AS transactions
            FROM tasks
            """)
            sample["host_at"] = utcnow()
            sample["sample_duration_ms"] = (time.perf_counter() - started) * 1000
            samples.append(sample)
        except Exception as error:
            samples.append({"host_at": utcnow(), "error": str(error)})
        # The sampler is primary evidence. Persist every observation so an
        # orchestrator interruption cannot erase a completed measurement window.
        write_json(path, samples)
        remaining = interval - (time.perf_counter() - started)
        stop.wait(max(0, remaining))
    write_json(path, samples)


def wait_terminal(timeout: float = 600) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last = task_counts()
    while time.monotonic() < deadline:
        last = task_counts()
        if last["queued"] == 0 and last["running"] == 0:
            return last
        time.sleep(0.5)
    raise TimeoutError(f"Черга не спорожніла: {last}")


def prometheus_query_range(query: str, start: float, end: float, step: int = 5) -> Any:
    params = urllib.parse.urlencode({"query": query, "start": start, "end": end, "step": step})
    with urllib.request.urlopen(f"{PROM_URL}/api/v1/query_range?{params}", timeout=30) as response:
        payload = json.loads(response.read())
    if payload["status"] != "success":
        raise RuntimeError(payload)
    return payload["data"]["result"]


def collect_evidence(run_dir: Path, started_epoch: float, ended_epoch: float) -> None:
    logs = subprocess.run(["kubectl", "-n", NAMESPACE, "logs", "-l", "app=worker",
                           "--prefix=true", "--tail=-1"], cwd=ROOT, text=True,
                          capture_output=True, encoding="utf-8", errors="replace")
    (run_dir / "worker.log").write_text(logs.stdout + logs.stderr, encoding="utf-8")
    pods = subprocess.run(["kubectl", "-n", NAMESPACE, "get", "pods", "-o", "json"],
                          cwd=ROOT, text=True, capture_output=True, encoding="utf-8")
    (run_dir / "pods.json").write_text(pods.stdout, encoding="utf-8")
    queries = {
        "db_cpu": 'rate(container_cpu_usage_seconds_total{namespace="lab4",pod=~"postgres-.*",container="postgres"}[30s])',
        "db_transactions": 'rate(pg_stat_database_xact_commit{datname="referrals"}[30s])+rate(pg_stat_database_xact_rollback{datname="referrals"}[30s])',
        "claim_buckets": 'sum by (le) (rate(worker_task_duration_seconds_bucket{step="claim"}[30s]))',
        "claim_p99_seconds": 'histogram_quantile(0.99, sum by (le) (rate(worker_task_duration_seconds_bucket{step="claim"}[30s])))',
        "claims_task_total": 'sum(worker_claims_total{result="task"})',
        "claims_empty_total": 'sum(worker_claims_total{result="empty"})',
        "worker_restarts": 'kube_pod_container_status_restarts_total{namespace="lab4",container="worker"}',
    }
    values = {}
    for key, query in queries.items():
        try:
            values[key] = prometheus_query_range(query, started_epoch, ended_epoch)
        except Exception as error:
            values[key] = {"error": str(error), "query": query}
    write_json(run_dir / "prometheus.json", values)


def collect_worker_metric_snapshot(run_dir: Path) -> None:
    pods_result = subprocess.run(
        ["kubectl", "-n", NAMESPACE, "get", "pods", "-l", "app=worker", "-o", "json"],
        cwd=ROOT, text=True, capture_output=True, encoding="utf-8", errors="replace",
        check=True,
    )
    pods = [item["metadata"]["name"] for item in json.loads(pods_result.stdout)["items"]
            if not item["metadata"].get("deletionTimestamp")]
    evidence: dict[str, Any] = {
        "at": utcnow(),
        "command_template": (
            "kubectl get --raw /api/v1/namespaces/lab4/pods/<pod>:9100/proxy/metrics"
        ),
        "pods": {},
    }
    totals = {"task": 0.0, "empty": 0.0}
    for pod in sorted(pods):
        endpoint = f"/api/v1/namespaces/{NAMESPACE}/pods/{pod}:9100/proxy/metrics"
        result = subprocess.run(
            ["kubectl", "get", "--raw", endpoint], cwd=ROOT, text=True,
            capture_output=True, encoding="utf-8", errors="replace",
        )
        values: dict[str, float] = {}
        if result.returncode == 0:
            for label in ("task", "empty"):
                match = re.search(
                    rf'^worker_claims_total\{{result="{label}"\}}\s+([0-9.eE+-]+)$',
                    result.stdout, re.MULTILINE,
                )
                values[label] = float(match.group(1)) if match else 0.0
                totals[label] += values[label]
        evidence["pods"][pod] = {
            "endpoint": endpoint,
            "exit_code": result.returncode,
            "values": values,
            "stderr": result.stderr,
        }
    evidence["totals"] = totals
    write_json(run_dir / "worker_metrics_snapshot.json", evidence)


def database_summary() -> dict[str, Any]:
    return psql_json("""
    SELECT COUNT(*)::int AS tasks,
      COUNT(*) FILTER (WHERE status='done')::int AS done,
      COUNT(*) FILTER (WHERE status='failed')::int AS failed,
      COUNT(*) FILTER (WHERE attempts>1)::int AS attempts_gt_1,
      COALESCE((SELECT COUNT(*) FROM (
        SELECT task_id FROM task_results GROUP BY task_id HAVING COUNT(*)>1
      ) d),0)::int AS duplicate_task_ids,
      (SELECT COUNT(*)::int FROM task_results) AS result_rows,
      COALESCE((SELECT json_object_agg(attempts,n) FROM (
        SELECT attempts,COUNT(*)::int AS n FROM tasks GROUP BY attempts ORDER BY attempts
      ) a),'{}'::json)::json AS attempts_distribution
    FROM tasks
    """)


def write_database_snapshot(run_dir: Path, suffix: str) -> None:
    task_raw = psql("COPY (SELECT row_to_json(t) FROM tasks t ORDER BY id) TO STDOUT;")
    result_raw = psql(
        "COPY (SELECT row_to_json(r) FROM task_results r ORDER BY task_id, created_at) TO STDOUT;"
    )
    for stem, content in (("tasks", task_raw), ("task_results", result_raw)):
        with (run_dir / f"{stem}_{suffix}.jsonl").open(
            "w", encoding="utf-8", newline="\n"
        ) as stream:
            if content:
                stream.write(content)
                stream.write("\n")


def capture_measurement_end(run_dir: Path) -> dict[str, Any]:
    measurement_end = {
        "at": utcnow(),
        "epoch": time.time(),
        "counts": task_counts(),
        "database": database_summary(),
    }
    write_database_snapshot(run_dir, "at_measurement_end")
    pods_result = subprocess.run(
        ["kubectl", "-n", NAMESPACE, "get", "pods", "-l", "app=worker", "-o", "json"],
        cwd=ROOT, text=True, capture_output=True, encoding="utf-8", errors="replace",
    )
    (run_dir / "pods_at_measurement_end.json").write_text(
        pods_result.stdout, encoding="utf-8"
    )
    payload = json.loads(pods_result.stdout)
    collected_logs = []
    for item in payload.get("items", []):
        pod = item["metadata"]["name"]
        for previous in (False, True):
            args = ["kubectl", "-n", NAMESPACE, "logs", pod, "--tail=-1"]
            if previous:
                args.append("--previous")
            result = subprocess.run(
                args, cwd=ROOT, text=True, capture_output=True,
                encoding="utf-8", errors="replace",
            )
            collected_logs.append(
                f"===== {pod} previous={previous} exit={result.returncode} =====\n"
                f"{result.stdout}{result.stderr}"
            )
    (run_dir / "worker_at_measurement_end.log").write_text(
        "\n".join(collected_logs), encoding="utf-8"
    )
    write_json(run_dir / "measurement_end.json", measurement_end)
    return measurement_end


def finish_run(run_dir: Path, config: dict[str, Any], started_epoch: float,
               rows: list[dict[str, Any]]) -> dict[str, Any]:
    ended_epoch = time.time()
    task_raw = psql("COPY (SELECT row_to_json(t) FROM tasks t ORDER BY id) TO STDOUT;") + "\n"
    result_raw = psql("COPY (SELECT row_to_json(r) FROM task_results r ORDER BY task_id, created_at) TO STDOUT;")
    if result_raw:
        result_raw += "\n"
    with (run_dir / "tasks.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(task_raw)
    with (run_dir / "task_results.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(result_raw)
    summary = {"generator_version": GENERATOR_VERSION, "config": config,
               "started_at_epoch": started_epoch, "ended_at_epoch": ended_epoch,
               "http": {"scheduled": len(rows),
                        "accepted": sum(row["status"] == 202 for row in rows),
                        "non_2xx": sum(row["status"] is not None and row["status"] != 202 for row in rows),
                        "transport_errors": sum(row["status"] is None for row in rows),
                        "schedule_lag_p95_ms": percentile([row["schedule_lag_ms"] for row in rows], 0.95)},
               "database": database_summary(),
               "primary_sha256": {
                   "requests.jsonl": hashlib.sha256((run_dir / "requests.jsonl").read_bytes()).hexdigest(),
                   "tasks.jsonl": hashlib.sha256((run_dir / "tasks.jsonl").read_bytes()).hexdigest(),
                   "task_results.jsonl": hashlib.sha256((run_dir / "task_results.jsonl").read_bytes()).hexdigest(),
               }}
    write_json(run_dir / "summary.json", summary)
    collect_evidence(run_dir, started_epoch, ended_epoch)
    return summary


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
    return ordered[index]


def simple_run(name: str, referrals: list[int], *, mode: str, poll_ms: int,
               replicas: int, count: int, rate: float, unique: bool = True,
               fail_rate: float = 0, batch: int = 1,
               priority: Callable[[int], int] | None = None,
               seed: Callable[[int], int | None] | None = None) -> dict[str, Any]:
    run_dir = RUNS / name
    run_dir.mkdir(parents=True, exist_ok=True)
    stop_workers(name)
    reset_tasks()
    set_result_uniqueness(unique)
    configure_workers(name + "_active", replicas, mode=mode, poll_ms=poll_ms,
                      fail_rate=fail_rate, batch=batch, idempotent=unique)
    stop = threading.Event()
    sampler = threading.Thread(target=sample_database,
                               args=(run_dir / "samples.json", stop), daemon=True)
    started_epoch = time.time()
    sampler.start()
    rows = generate_tasks(run_dir / "requests.jsonl", referrals, count, rate, priority, seed)
    wait_terminal()
    stop.set(); sampler.join()
    collect_worker_metric_snapshot(run_dir)
    return finish_run(run_dir, {"mode": mode, "poll_ms": poll_ms, "replicas": replicas,
                                "count": count, "rate": rate, "unique": unique,
                                "fail_rate": fail_rate, "batch": batch}, started_epoch, rows)


def api_check(referrals: list[int]) -> None:
    reset_tasks()
    checks = [
        http("POST", f"/referrals/{referrals[0]}/tasks", {"priority": 3, "seed": 123}),
        http("GET", "/tasks?limit=20&offset=0&status=queued"),
        http("GET", "/tasks/1"),
        http("POST", "/referrals/9223372036854775807/tasks", {"priority": 2}),
        http("POST", f"/referrals/{referrals[0]}/tasks", {"priority": 0}),
        http("POST", f"/referrals/{referrals[0]}/tasks", {"priority": 4}),
        http("POST", f"/referrals/{referrals[0]}/tasks", {"seed": 2147483648}),
        http("GET", "/tasks/9223372036854775808"),
    ]
    metrics = urllib.request.urlopen(BASE_URL + "/metrics", timeout=10).read().decode()
    write_json(OUT / "http" / "api_check.json", checks)
    (OUT / "http" / "metrics.txt").write_text(metrics, encoding="utf-8")
    write_json(OUT / "schema_check.json", {
        "tables": psql("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename;"),
        "constraints": psql("SELECT conname||':'||pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid IN ('tasks'::regclass,'task_results'::regclass) ORDER BY conname;"),
        "indexes": psql("SELECT indexname||':'||indexdef FROM pg_indexes WHERE tablename IN ('tasks','task_results') ORDER BY indexname;"),
    })


def phase_naive(referrals: list[int]) -> None:
    simple_run("naive_100ms", referrals, mode="naive", poll_ms=100, replicas=R,
               count=300, rate=100, unique=False)
    simple_run("naive_1ms", referrals, mode="naive", poll_ms=1, replicas=R,
               count=300, rate=100, unique=False)


def phase_claims(referrals: list[int]) -> None:
    phase_naive(referrals)
    simple_run("skip_locked_1ms", referrals, mode="skip_locked", poll_ms=1, replicas=R,
               count=300, rate=100, unique=True)
    simple_run("locked_1ms", referrals, mode="locked", poll_ms=1, replicas=R,
               count=300, rate=100, unique=True)


def phase_worker_smoke(referrals: list[int]) -> None:
    simple_run("worker20", referrals, mode="skip_locked", poll_ms=100, replicas=1,
               count=20, rate=10)


def phase_lease(referrals: list[int]) -> None:
    name = "lease_fencing"
    run_dir = RUNS / name
    stop_workers(name)
    reset_tasks(); set_result_uniqueness(True)
    configure_workers(name + "_active", 2)
    stop = threading.Event()
    sampler = threading.Thread(target=sample_database,
                               args=(run_dir / "samples.json", stop, 0.5), daemon=True)
    started_epoch = time.time(); sampler.start()
    pods_payload = json.loads(subprocess.run(
        ["kubectl", "-n", NAMESPACE, "get", "pods", "-l", "app=worker", "-o", "json"],
        cwd=ROOT, text=True, capture_output=True, encoding="utf-8", check=True,
    ).stdout)
    pods = sorted(
        item["metadata"]["name"] for item in pods_payload["items"]
        if not item["metadata"].get("deletionTimestamp")
        and item.get("status", {}).get("phase") == "Running"
        and any(condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in item.get("status", {}).get("conditions", []))
    )
    if len(pods) != 2:
        raise RuntimeError(f"Очікувалося 2 готових worker pod, отримано {pods}")
    probe_stop = kubectl("lease_kubectl_stop_probe", ["-n", NAMESPACE, "exec", pods[0],
                                                        "--", "kill", "-STOP", "1"])
    probe_state = kubectl("lease_kubectl_stop_state", ["-n", NAMESPACE, "exec", pods[0],
                                                         "--", "sh", "-c",
                                                         "grep '^State:' /proc/1/status"])
    kubectl("lease_kubectl_cont_probe", ["-n", NAMESPACE, "exec", pods[0],
                                          "--", "kill", "-CONT", "1"])
    write_json(run_dir / "kubectl_stop_probe.json", {
        "command_exit_code": probe_stop.returncode,
        "observed_proc_state": probe_state.stdout.strip(),
        "conclusion": "kubectl exec kill -STOP 1 returned 0 but /proc/1 did not enter T (stopped); runtime PID signal used for the measured run",
    })
    holder: dict[str, list[dict[str, Any]]] = {}
    generator = threading.Thread(
        target=lambda: holder.setdefault("rows", generate_tasks(
            run_dir / "requests.jsonl", referrals, 100, 10)), daemon=True)
    generator.start()
    before = None; pod = None; stopped_at = None
    for attempt in range(20):
        time.sleep(0.04)
        candidate = pods[attempt % len(pods)]
        runtime_signal(f"lease_stop_{attempt}", candidate, "STOP")
        text = psql(f"SELECT row_to_json(q) FROM (SELECT * FROM tasks WHERE status='running' AND worker_id='{candidate}' ORDER BY started_at LIMIT 1) q;")
        if text:
            before = json.loads(text); pod = candidate; stopped_at = utcnow(); break
        runtime_signal(f"lease_continue_retry_{attempt}", candidate, "CONT")
    if before is None or pod is None:
        generator.join()
        raise RuntimeError("20 зовнішніх спроб не потрапили у running; прогін відхилено")
    time.sleep(10)
    during = psql_json(f"SELECT * FROM tasks WHERE id={int(before['id'])}")
    runtime_signal("lease_continue", pod, "CONT")
    generator.join()
    wait_terminal()
    after = psql_json(f"""
      SELECT t.*, (SELECT COUNT(*)::int FROM task_results r WHERE r.task_id=t.id) AS result_rows
      FROM tasks t WHERE t.id={int(before['id'])}
    """)
    stop.set(); sampler.join()
    write_json(run_dir / "lease_case.json", {"pod": pod, "stopped_at": stopped_at,
                                               "continued_at": utcnow(), "before": before,
                                               "during": during, "after": after})
    finish_run(run_dir, {"workers": 2, "tasks": 100, "stop_seconds": 10,
                         "lease_seconds": 5}, started_epoch, holder["rows"])


def capacity_run(name: str, referrals: list[int], workers: int,
                 backlog: int = 400) -> dict[str, Any]:
    run_dir = RUNS / name
    stop_workers(name)
    reset_tasks(); set_result_uniqueness(True)
    rows = generate_tasks(run_dir / "requests.jsonl", referrals, backlog, 200)
    started_epoch = time.time()
    stop = threading.Event()
    sampler = threading.Thread(target=sample_database,
                               args=(run_dir / "samples.json", stop, 0.5), daemon=True)
    sampler.start()
    configure_workers(name + "_active", workers)
    wait_terminal()
    stop.set(); sampler.join()
    collect_worker_metric_snapshot(run_dir)
    return finish_run(run_dir, {"workers": workers, "backlog": backlog}, started_epoch, rows)


def phase_capacity(referrals: list[int]) -> None:
    summaries = {workers: capacity_run(f"capacity_w{workers}", referrals, workers)
                 for workers in (1, 2, R)}
    write_json(OUT / "capacity_runs.json", summaries)


def phase_capacity30(referrals: list[int]) -> None:
    backlogs = {1: 800, 2: 1600, R: 4000}
    summaries = {
        workers: capacity_run(
            f"capacity30_w{workers}", referrals, workers, backlogs[workers]
        )
        for workers in (1, 2, R)
    }
    write_json(OUT / "capacity30_runs.json", summaries)


def load_for_duration(name: str, referrals: list[int], rate: float, duration: int,
                      *, fail_rate: float = 0, poison_at: int | None = None,
                      priority: Callable[[int], int] | None = None,
                      action: Callable[[], None] | None = None,
                      action_at: int | None = None,
                      controlled_drain: bool = False) -> dict[str, Any]:
    run_dir = RUNS / name
    stop_workers(name)
    reset_tasks(); set_result_uniqueness(True)
    configure_workers(name, R, fail_rate=fail_rate)
    stop = threading.Event()
    sampler = threading.Thread(target=sample_database,
                               args=(run_dir / "samples.json", stop), daemon=True)
    started_epoch = time.time(); sampler.start()
    actions = []
    if action and action_at is not None:
        def delayed_action():
            time.sleep(action_at); action()
        thread = threading.Thread(target=delayed_action, daemon=True); thread.start(); actions.append(thread)
    count = max(1, round(rate * duration))
    if poison_at is None:
        seed_fn = None; effective_priority = priority
    else:
        poison_start = round(rate * poison_at)
        seed_fn = lambda i: 0 if poison_start <= i < poison_start + 10 else None
        effective_priority = lambda i: 3 if poison_start <= i < poison_start + 10 else (
            priority(i) if priority else 2)
    rows = generate_tasks(run_dir / "requests.jsonl", referrals, count, rate,
                          effective_priority, seed_fn)
    for thread in actions: thread.join()
    if controlled_drain:
        capture_measurement_end(run_dir)
        configure_workers(name + "_controlled_drain", R, fail_rate=0)
    wait_terminal(timeout=max(600, duration * 3))
    stop.set(); sampler.join()
    return finish_run(run_dir, {"rate": rate, "duration": duration,
                                "fail_rate": fail_rate, "poison_at": poison_at,
                                "controlled_drain": controlled_drain},
                      started_epoch, rows)


def measured_x5() -> float:
    analysis = OUT / "analysis.json"
    if analysis.exists():
        return float(json.loads(analysis.read_text(encoding="utf-8"))["capacity"]["5"]["x"])
    summary = RUNS / "capacity_w5" / "summary.json"
    if not summary.exists():
        raise RuntimeError("Спочатку виконайте phase capacity та analyze_lab4.py")
    tasks = psql("SELECT 1")
    del tasks
    samples = json.loads((RUNS / "capacity_w5" / "samples.json").read_text(encoding="utf-8"))
    valid = [row for row in samples if "done" in row]
    if len(valid) < 2:
        raise RuntimeError("Недостатньо вибірок capacity_w5")
    return max(0.1, (valid[-1]["done"] - valid[0]["done"]) /
               ((datetime.fromisoformat(valid[-1]["db_at"]) - datetime.fromisoformat(valid[0]["db_at"])).total_seconds()))


def phase_queue(referrals: list[int], x5: float) -> None:
    load_for_duration("little_steady", referrals, 0.7 * x5, 180)
    load_for_duration("little_overload", referrals, 1.3 * x5, 120)


def phase_shutdown(referrals: list[int], x5: float) -> None:
    run_dir = RUNS / "graceful_restart"
    def restart():
        old_payload = json.loads(subprocess.run(
            ["kubectl", "-n", NAMESPACE, "get", "pods", "-l", "app=worker", "-o", "json"],
            cwd=ROOT, text=True, capture_output=True, encoding="utf-8", check=True,
        ).stdout)
        old_pods = [item["metadata"]["name"] for item in old_payload["items"]
                    if not item["metadata"].get("deletionTimestamp")]
        kubectl("shutdown_rollout_restart", ["-n", NAMESPACE, "rollout", "restart", "deployment/worker"])
        observations = []
        old_logs: dict[str, str] = {}
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and old_pods:
            remaining = []
            for pod in old_pods:
                result = subprocess.run(["kubectl", "-n", NAMESPACE, "get", "pod", pod, "-o", "json"],
                                        cwd=ROOT, text=True, capture_output=True, encoding="utf-8")
                if result.returncode == 0:
                    remaining.append(pod)
                    observations.append({"at": utcnow(), "pod": pod,
                                         "object": json.loads(result.stdout)})
                    logs = subprocess.run(["kubectl", "-n", NAMESPACE, "logs", pod, "--tail=-1"],
                                          cwd=ROOT, text=True, capture_output=True, encoding="utf-8")
                    if logs.returncode == 0: old_logs[pod] = logs.stdout
            old_pods = remaining
            time.sleep(0.25)
        kubectl("shutdown_rollout_status", ["-n", NAMESPACE, "rollout", "status",
                                             "deployment/worker", "--timeout=300s"])
        write_json(run_dir / "rollout_observation.json", observations)
        (run_dir / "old_pods.log").write_text(
            "\n".join(f"===== {pod} =====\n{text}" for pod, text in old_logs.items()),
            encoding="utf-8")
    load_for_duration("graceful_restart", referrals, 0.7 * x5, 120,
                      action=restart, action_at=60)


def phase_failures(referrals: list[int], x5: float) -> None:
    load_for_duration("fail_baseline", referrals, 0.7 * x5, 300,
                      fail_rate=0.15, controlled_drain=True)
    load_for_duration("fail_poison", referrals, 0.7 * x5, 300,
                      fail_rate=0.15, poison_at=150, controlled_drain=True)
    phase_poison(referrals, x5)


def f3_evidence_run(name: str, referrals: list[int], *, poison: bool) -> dict[str, Any]:
    run_dir = RUNS / name
    stop_workers(name)
    reset_tasks(); set_result_uniqueness(True)
    configure_workers(name, R, fail_rate=0.15)
    stop = threading.Event()
    sampler = threading.Thread(
        target=sample_database, args=(run_dir / "samples.json", stop), daemon=True
    )
    sampler.start()
    rate = 0.2
    duration = 300
    poison_at = 150
    normal_count = round(rate * duration)
    started_epoch = time.time()
    origin = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        normal_future = executor.submit(
            generate_tasks, None, referrals, normal_count, rate,
            None, None, origin=origin,
        )
        poison_future = None
        if poison:
            poison_future = executor.submit(
                generate_tasks, None, referrals, 10, 1000.0,
                lambda _index: 3, lambda _index: 0,
                origin=origin, start_offset=poison_at, index_offset=normal_count,
            )
        normal_rows = normal_future.result()
        poison_rows = poison_future.result() if poison_future is not None else []
    remaining = origin + duration - time.perf_counter()
    if remaining > 0:
        time.sleep(remaining)
    rows = sorted(normal_rows + poison_rows, key=lambda row: row["scheduled_offset_ms"])
    write_request_rows(run_dir / "requests.jsonl", rows)
    capture_measurement_end(run_dir)
    stop.set(); sampler.join()
    measured_tasks = [
        json.loads(line)
        for line in (run_dir / "tasks_at_measurement_end.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        if line
    ]
    measured_poison = [row for row in measured_tasks if row.get("seed") == 0]
    poison_accepted = (
        not poison or (
            len(measured_poison) == 10
            and all(row["status"] == "failed" and int(row["attempts"]) == 3
                    for row in measured_poison)
        )
    )
    configure_workers(name + "_controlled_drain", R, fail_rate=0)
    wait_terminal(timeout=600)
    summary = finish_run(
        run_dir,
        {"rate": rate, "duration": duration, "fail_rate": 0.15,
         "poison_at": poison_at if poison else None, "poison_burst": poison,
         "normal_count": normal_count, "poison_count": len(poison_rows),
         "controlled_drain": True},
        started_epoch, rows,
    )
    if not poison_accepted:
        raise RuntimeError(
            f"Ф3-прогін не пройшов критерій: "
            f"poison at measurement end={[(row['status'], row['attempts']) for row in measured_poison]}"
        )
    return summary


def phase_poison(referrals: list[int], _x5: float) -> None:
    f3_evidence_run("f3_baseline", referrals, poison=False)
    f3_evidence_run("f3_poison", referrals, poison=True)


def phase_priority(referrals: list[int], x5: float) -> None:
    load_for_duration("priority_d1", referrals, 1.3 * x5, 120,
                      priority=lambda index: index % 3 + 1)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--phase", choices=("prepare", "api", "worker", "naive", "claims", "lease", "capacity", "capacity30",
                                             "queue", "shutdown", "failures", "poison", "priority", "all"),
                        required=True)
    result.add_argument("--x5", type=float)
    return result


def main() -> None:
    args = parser().parse_args()
    OUT.mkdir(parents=True, exist_ok=True); RUNS.mkdir(parents=True, exist_ok=True)
    referrals = prepare_referrals()
    if args.phase in {"api", "all"}: api_check(referrals)
    if args.phase in {"worker", "all"}: phase_worker_smoke(referrals)
    if args.phase == "naive": phase_naive(referrals)
    if args.phase in {"claims", "all"}: phase_claims(referrals)
    if args.phase in {"lease", "all"}: phase_lease(referrals)
    if args.phase in {"capacity", "all"}: phase_capacity(referrals)
    if args.phase in {"capacity30", "all"}: phase_capacity30(referrals)
    if args.phase in {"queue", "shutdown", "failures", "poison", "priority", "all"}:
        x5 = args.x5 or measured_x5()
        if args.phase in {"queue", "all"}: phase_queue(referrals, x5)
        if args.phase in {"shutdown", "all"}: phase_shutdown(referrals, x5)
        if args.phase in {"failures", "all"}: phase_failures(referrals, x5)
        if args.phase == "poison": phase_poison(referrals, x5)
        if args.phase in {"priority", "all"}: phase_priority(referrals, x5)
    write_json(OUT / "orchestrator_last_run.json", {"finished_at": utcnow(),
                                                     "phase": args.phase,
                                                     "generator_version": GENERATOR_VERSION})


if __name__ == "__main__":
    main()

