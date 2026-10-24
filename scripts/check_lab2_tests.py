"""Run regression suite and store its actual command/output for the report."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
COMMAND=[sys.executable,"-m","unittest","discover","-s","tests","-v"]
result=subprocess.run(COMMAND,cwd=ROOT,text=True,stdout=subprocess.PIPE,
                      stderr=subprocess.STDOUT,encoding="utf-8",errors="replace")
path=ROOT/"results"/os.getenv("LAB2_RUN_NAME","lab2_final")/"tests.json"
path.write_text(json.dumps({"command":COMMAND,"exit_code":result.returncode,
                            "output":result.stdout},ensure_ascii=False,indent=2),encoding="utf-8")
print(result.stdout[-1200:])
raise SystemExit(result.returncode)
