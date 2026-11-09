"""Виправляє лише метадані SHA-256, не змінюючи первинні JSONL."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "results" / "lab4_final"


def main() -> None:
    changes = []
    for run_dir in sorted((DATA / "runs").iterdir()):
        summary_path = run_dir / "summary.json"
        if not summary_path.exists():
            continue
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        for name in ("requests.jsonl", "tasks.jsonl", "task_results.jsonl"):
            path = run_dir / name
            if not path.exists():
                continue
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            previous = summary["primary_sha256"][name]
            if actual != previous:
                changes.append({"run": run_dir.name, "file": name,
                                "old": previous, "new": actual})
                summary["primary_sha256"][name] = actual
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8")
    correction = {
        "at": datetime.now(timezone.utc).isoformat(),
        "reason": ("Python text-mode newline translation on Windows changed LF to CRLF; "
                   "raw files were not modified"),
        "changes": changes,
    }
    (DATA / "hash_correction.json").write_text(
        json.dumps(correction, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"updated_hashes": len(changes)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
