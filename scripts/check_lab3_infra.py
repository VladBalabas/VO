"""Record live, non-destructive Kubernetes and Prometheus checks for Lab 3."""
from __future__ import annotations

import json
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results" / "lab3_final" / "infra_check.json"


def shell(args: list[str]) -> dict:
    result = subprocess.run(
        args,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
    )
    return {"command": args, "exit_code": result.returncode, "output": result.stdout}


def http(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=15) as response:
        return {
            "url": url,
            "status": response.status,
            "headers": dict(response.headers),
            "body": response.read().decode("utf-8", errors="replace"),
        }


def prometheus_query(expression: str) -> dict:
    url = "http://localhost:9090/api/v1/query?" + urllib.parse.urlencode({"query": expression})
    response = http(url)
    response["expression"] = expression
    response["parsed"] = json.loads(response["body"])
    return response


def result_count(response: dict) -> int:
    return len(response["parsed"]["data"]["result"])


def main() -> None:
    commands = [
        shell(["kubectl", "config", "use-context", "kind-vo-lab3"]),
        shell(["kubectl", "apply", "-f", "k8s/hpa-30.yaml"]),
    ]
    if any(item["exit_code"] for item in commands):
        raise RuntimeError("Cannot select the cluster or apply the HPA")

    # Create fresh application counter samples and allow two 5 s scrapes plus HPA polling.
    requests = [http("http://localhost:8080/livez") for _ in range(20)]
    time.sleep(20)
    commands.extend([
        shell(["kind", "get", "clusters"]),
        shell(["kubectl", "get", "nodes", "-o", "wide"]),
        shell(["kubectl", "get", "pods", "-A", "-o", "wide"]),
        shell(["kubectl", "-n", "lab3", "get", "statefulset,deployment,service,pvc,job,hpa"]),
        shell(["kubectl", "-n", "monitoring", "get", "podmonitor"]),
        shell(["kubectl", "-n", "lab3", "top", "pods"]),
        shell(["kubectl", "-n", "lab3", "get", "hpa", "web", "-o", "wide"]),
    ])

    expressions = {
        "web_requests": 'http_requests_total{namespace="lab3",pod=~"web-.*"}',
        "web_cfs_periods": (
            'container_cpu_cfs_periods_total{namespace="lab3",container="web",'
            'metrics_path="/metrics/cadvisor"}'
        ),
        "ready_replicas": (
            'kube_deployment_status_replicas_ready{namespace="lab3",deployment="web"}'
        ),
        "desired_replicas": (
            'kube_horizontalpodautoscaler_status_desired_replicas{namespace="lab3",'
            'horizontalpodautoscaler="web"}'
        ),
        "postgres_exporter": 'pg_up{namespace="lab3"}',
    }
    queries = {name: prometheus_query(expression) for name, expression in expressions.items()}
    targets_response = http("http://localhost:9090/api/v1/targets?state=active")
    targets = json.loads(targets_response["body"])["data"]["activeTargets"]
    lab_targets = [
        target for target in targets
        if target.get("labels", {}).get("namespace") == "lab3"
    ]
    required_targets = [
        target for target in lab_targets
        if target.get("labels", {}).get("job", "").split("/")[-1]
        in {"web", "postgres-exporter"}
    ]
    required_down = [target for target in required_targets if target.get("health") != "up"]
    unrelated_down = [
        {
            "scrapePool": target.get("scrapePool"),
            "labels": target.get("labels"),
            "health": target.get("health"),
            "lastError": target.get("lastError"),
        }
        for target in targets
        if target.get("health") != "up" and target not in required_targets
    ]
    health = [http("http://localhost:8080/livez"), http("http://localhost:8080/healthz")]
    cleanup = shell(["kubectl", "-n", "lab3", "delete", "hpa", "web", "--ignore-not-found"])
    commands.append(cleanup)

    checks = {
        "commands_succeeded": all(item["exit_code"] == 0 for item in commands),
        "health_endpoints": all(item["status"] == 200 for item in health),
        "all_distribution_requests_have_x_pod": all(
            any(key.lower() == "x-pod" for key in item["headers"]) for item in requests
        ),
        "required_lab_targets_up": bool(required_targets) and not required_down,
        "web_request_series": result_count(queries["web_requests"]) >= 1,
        "web_cfs_series": result_count(queries["web_cfs_periods"]) >= 1,
        "ready_replica_series": result_count(queries["ready_replicas"]) == 1,
        "desired_replica_series": result_count(queries["desired_replicas"]) == 1,
        "postgres_exporter_series": result_count(queries["postgres_exporter"]) >= 1,
        "kubectl_top": commands[-3]["exit_code"] == 0 and "web-" in commands[-3]["output"],
    }
    result = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "checks_passed": all(checks.values()),
        "checks": checks,
        "commands": commands,
        "health": health,
        "distribution_requests": requests,
        "queries": queries,
        "required_lab_targets": required_targets,
        "required_lab_targets_down": required_down,
        "unrelated_targets_down": unrelated_down,
        "scope_note": (
            "Only web and postgres-exporter are Lab 3 application targets. "
            "Unrelated kind control-plane targets are reported, not hidden."
        ),
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"checks_passed": result["checks_passed"], "checks": checks,
                      "unrelated_down": len(unrelated_down)}, indent=2))
    if not result["checks_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
