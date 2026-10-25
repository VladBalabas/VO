"""Final acceptance audit for Lab 3 requirements, evidence and report links."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "results" / "lab3_final"


def load(name: str):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def contains(path: str, *needles: str) -> bool:
    text = (ROOT / path).read_text(encoding="utf-8")
    return all(needle in text for needle in needles)


def main() -> None:
    checks: dict[str, bool] = {}
    analysis = load("analysis.json")
    verification = load("independent_verification.json")
    infra = load("infra_check.json")
    security = load("security_check.json")
    tests = load("tests.json")
    raw_manifest = load("raw_manifest.json")

    checks["preimplementation_design"] = (ROOT / "LAB3_DESIGN.md").exists()
    checks["cluster_1_control_plane_2_workers_and_ports"] = contains(
        "k8s/kind-cluster.yaml", "role: control-plane", "role: worker", "8080", "9090"
    ) and (ROOT / "k8s/kind-cluster.yaml").read_text(encoding="utf-8").count("role: worker") == 2
    checks["postgres_stateful_pvc_headless_max100"] = contains(
        "k8s/postgres.yaml", "kind: StatefulSet", "clusterIP: None", "volumeClaimTemplates", "max_connections=100"
    )
    checks["migration_job"] = contains("k8s/migration-job.yaml", "kind: Job")
    checks["web_required_fields"] = contains(
        "k8s/web.yaml", "kind: Deployment", "DB_POOL_SIZE", "secretKeyRef", "metadata.name",
        "250m", "500m", "512Mi", "readinessProbe", "/healthz", "timeoutSeconds: 3",
        "periodSeconds: 5", "failureThreshold: 3", "livenessProbe", "/livez",
        "terminationGracePeriodSeconds: 15", "sleep:", "seconds: 5", "type: NodePort"
    ) and "replicas:" not in (ROOT / "k8s/web.yaml").read_text(encoding="utf-8")
    checks["monitoring_5s_and_components"] = contains(
        "k8s/monitoring-values.yaml", "scrapeInterval: 5s", "nodeSelector", "role: infra"
    ) and contains("k8s/monitoring-resources.yaml", "kind: PodMonitor", "postgres-exporter")
    checks["metrics_server_insecure_tls"] = contains(
        "k8s/metrics-server-values.yaml", "--kubelet-insecure-tls"
    )
    checks["deploy_and_delete_commands"] = all(
        (ROOT / name).exists() for name in ("scripts/deploy_lab3.ps1", "scripts/delete_lab3.ps1")
    ) and all(
        "PowerShell transcript end" in (DATA / name).read_text(encoding="utf-8")
        for name in ("commands/deploy.log", "commands/delete.log")
    ) and "absence verified" in (DATA / "commands/delete.log").read_text(encoding="utf-8")
    checks["crud_full_bodies_and_x_pod"] = (
        [row["status"] for row in load("http/crud_check.json") if row["path"].startswith("/referrals")]
        == [201, 200, 200, 204, 404]
        and all("response_body" in row and "request_body" in row for row in load("http/crud_check.json"))
        and all(any(key.lower() == "x-pod" for key in row["response_headers"])
                for row in load("http/crud_check.json"))
    )
    persistence = load("http/persistence_check.json")
    checks["postgres_persistence"] = (
        persistence["different_pod_instance"]
        and persistence["requests"][-1]["status"] == 200
    )
    checks["secret_absent"] = security["passed"]
    checks["live_monitoring_evidence"] = infra["checks_passed"]
    checks["full_regression_suite"] = tests["exit_code"] == 0 and "Ran 23 tests" in tests["output"]

    checks["raw_manifest_nine_runs"] = (
        len(raw_manifest["files"]) == 9
        and {row["run"] for row in raw_manifest["files"]}
        == {"noop", "n1", "n2", "n3", "n5", "fixed_profile", "hpa30_profile",
            "d5_hpa30", "d5_hpa300"}
        and all(len(row["sha256"]) == 64 and row["records"] > 0 for row in raw_manifest["files"])
    )
    checks["independent_raw_recalculation"] = (
        verification["verification_passed"]
        and sum(row["raw_records"] for row in verification["runs"]) == 1_319_938
    )
    checks["generator_40_connections_rotate_100"] = (
        verification["definitions"]["connections"].startswith("40 workers")
        and all(row["observed_sessions"] >= 40 for row in verification["runs"])
    )
    fixed = analysis["fixed_table"]
    checks["fixed_n_1_2_3_5"] = [row["n"] for row in fixed] == [1, 2, 3, 5]
    checks["fixed_metrics_and_no_errors"] = all(
        row["x"] > 0
        and row["stddev"] >= 0
        and row["client_p99_ms"] > 0
        and row["mean_throttling"] is not None
        and row["max_pod_throttling"] is not None
        and row["non_2xx"] == row["timeouts"] == row["connection_errors"] == 0
        for row in fixed
    )
    checks["standalone_generator_ceiling_recorded"] = all(
        not row["generator_limited"] and row["x"] < .9 * analysis["generator_G"] for row in fixed
    )
    checks["imbalance_and_per_pod_detail"] = all(
        row["imbalance"] >= 1 and str(row["n"]) in analysis["pod_details"]
        for row in fixed if row["n"] > 1
    )
    usl = analysis["usl"]
    checks["usl_fit_and_n4"] = (
        usl is not None and usl["sigma"] >= 0 and usl["kappa"] >= 0
        and math.isclose(analysis["capacities_for_elasticity"]["4"], usl["predicted_x"]["4"])
    )
    short = analysis["elasticity"]
    checks["short_profiles_equal_t"] = short["fixed_profile"]["T"] == short["hpa30_profile"]["T"] == 180
    checks["short_profile_no_http_errors"] = all(
        short[name][key] == 0
        for name in ("fixed_profile", "hpa30_profile")
        for key in ("http_non_2xx", "timeouts", "connection_errors")
    )
    checks["hpa_windows_and_limits"] = (
        contains("k8s/hpa-30.yaml", "minReplicas: 1", "maxReplicas: 5", "averageUtilization: 80",
                 "stabilizationWindowSeconds: 30")
        and contains("k8s/hpa-300.yaml", "stabilizationWindowSeconds: 300")
    )
    checks["hpa_stable_60s_before_profiles"] = all(
        load(f"stability/{name}.json")["passed"]
        for name in ("hpa30_profile", "d5_hpa30", "d5_hpa300")
    )
    checks["d5_equal_450s_and_window_effect"] = (
        short["d5_hpa30"]["T"] == short["d5_hpa300"]["T"] == 450
        and analysis["hpa_events"]["d5_hpa30"]["first_ready_decrease_after_jump_end_seconds"]
        < analysis["hpa_events"]["d5_hpa300"]["first_ready_decrease_after_jump_end_seconds"]
    )
    checks["required_figures"] = all(
        (DATA / "figures" / name).exists()
        for name in (
            "usl_compose_kubernetes.png", "n5_per_pod_requests.png",
            "short_fixed_profile.png", "short_hpa30_profile.png",
            "d5_d5_hpa30.png", "d5_d5_hpa300.png",
        )
    )

    report = (ROOT / "REPORT_L3.md").read_text(encoding="utf-8")
    links = [link for link in re.findall(r"!?\[[^]]*\]\(([^)]+)\)", report)
             if "://" not in link and not link.startswith("#")]
    missing = [link for link in links if not (ROOT / link).exists()]
    checks["report_links"] = bool(links) and not missing
    checks["report_ai_disclosure"] = "генератив" in report.lower() and "незалеж" in report.lower()
    checks["report_three_alternatives"] = all(
        marker in report for marker in ("Альтернатива 1", "Альтернатива 2", "Альтернатива 3")
    )
    checks["report_explicit_limitations"] = "Обмеження" in report and "не досягнуто" in report
    checks["report_generator_lag_disclosed"] = "schedule lag" in report.lower() and "21 967" in report

    result = {
        "audit_passed": all(checks.values()),
        "checks": checks,
        "requirement_count": len(checks),
        "failed": [name for name, passed in checks.items() if not passed],
        "report_link_count": len(links),
        "missing_links": missing,
        "explicit_limitations": [
            "HPA did not reach its maximum ready-replica event within the 60 s spike",
            "p99 did not recover for 10 consecutive seconds during the 60 s spike",
            "kind control-plane scrape targets unrelated to the lab application may be down",
            "N*=46.44 is an extrapolation far outside the measured N<=5 range",
        ],
    }
    (DATA / "requirements_audit.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["audit_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
