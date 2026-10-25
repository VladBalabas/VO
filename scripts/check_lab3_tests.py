"""Run the full regression suite and preserve the exact command and output."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results" / "lab3_final" / "tests.json"
COMMAND = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]


def main() -> None:
    result = subprocess.run(
        COMMAND,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
    )
    payload = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "command": COMMAND,
        "exit_code": result.returncode,
        "output": result.stdout,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(result.stdout)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
