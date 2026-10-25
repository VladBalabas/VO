"""Re-query the saved Lab 3 time windows without rerunning any load test."""
from __future__ import annotations

import json
from pathlib import Path

from run_lab3 import collect_metrics


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "results" / "lab3_final"
RUNS = ("noop", "n1", "n2", "n3", "n5", "fixed_profile", "hpa30_profile",
        "d5_hpa30", "d5_hpa300")


def main() -> None:
    for name in RUNS:
        summary = json.loads(
            (DATA / "raw" / f"{name}.summary.json").read_text(encoding="utf-8")
        )
        collect_metrics(name, summary)
        print(f"refreshed historical Prometheus window: {name}")


if __name__ == "__main__":
    main()
