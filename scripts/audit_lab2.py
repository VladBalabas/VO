"""Final acceptance audit for Lab 2 evidence and report links."""
from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "results" / "lab2_final"


def load(name: str):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def main() -> None:
    checks: dict[str, bool] = {}
    checks["independent_recalculation"] = load("independent_verification.json")["verification_passed"]
    checks["regression_tests"] = load("tests.json")["exit_code"] == 0
    checks["api_full_log"] = load("api_check.json")["completed"] and len(load("api_check.json")["checks"]) >= 30
    shutdown = load("graceful_stop_r100.json")
    checks["graceful_shutdown"] = (
        shutdown["web_exit_code"] == "0"
        and shutdown["responses_non_2xx"] == 0
        and shutdown["connection_errors"] == 0
    )
    liveness = load("liveness_check.json")
    checks["liveness_independent"] = (
        [x["status"] for x in liveness["during_db_stop"]] == [200, 503]
    )
    infra = load("infra_check.json")
    targets = json.loads(infra["http"][-1]["body"])["data"]["activeTargets"]
    checks["four_targets_up"] = len([
        target for target in targets
        if target["labels"].get("job") == "web" and target["health"] == "up"
    ]) == 4
    checks["four_pods_receive_requests"] = (
        len(infra["pod_counts"]) == 4 and sum(infra["pod_counts"].values()) == 40
    )
    ceiling = load("generator_ceiling.json")
    checks["generator_two_miss_ceiling"] = (
        ceiling["stop_two_misses"]
        and ceiling["steps"][-1]["x"] < .9 * ceiling["steps"][-1]["rate"]
        and ceiling["steps"][-2]["x"] < .9 * ceiling["steps"][-2]["rate"]
    )
    checks["four_final_series"] = all(
        (DATA / "raw" / f"n{n}_profile.json").exists()
        and (DATA / "series" / f"n{n}.json").exists()
        for n in range(1, 5)
    )
    checks["required_figures"] = all(
        (DATA / "figures" / name).exists()
        for name in ("n1_timeline.png", "n4_timeline.png", "usl.png", "postgres_variant.png")
    )
    report = (ROOT / "REPORT_L2.md").read_text(encoding="utf-8")
    links = [link for link in re.findall(r"!?\[[^]]*\]\(([^)]+)\)", report)
             if "://" not in link and not link.startswith("#")]
    missing = [link for link in links if not (ROOT / link).exists()]
    checks["report_links"] = not missing
    result = {"audit_passed": all(checks.values()), "checks": checks,
              "report_link_count": len(links), "missing_links": missing,
              "explicit_limitations": [
                  "USL fit has high RMSE",
                  "single common cause of N=4 degradation is not causally isolated",
              ]}
    (DATA / "requirements_audit.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["audit_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
