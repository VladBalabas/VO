"""Verify that the PostgreSQL password is absent from tracked/config/image data."""
from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results" / "lab3_final" / "security_check.json"


def run(args: list[str]) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(args, cwd=ROOT, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace"))
    return result


def main() -> None:
    environment = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            environment[key.strip()] = value.strip()
    password = environment["POSTGRES_PASSWORD"]
    if not password:
        raise AssertionError("POSTGRES_PASSWORD is empty")

    repository_files = run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"]
    ).stdout.split(b"\0")
    matches = []
    needle = password.encode()
    for raw_name in repository_files:
        if not raw_name:
            continue
        path = ROOT / raw_name.decode("utf-8", errors="surrogateescape")
        if path.is_file() and needle in path.read_bytes():
            matches.append(path.relative_to(ROOT).as_posix())

    image = run(["docker", "image", "inspect", "vo-web:lab3"]).stdout
    configmap = run(["kubectl", "-n", "lab3", "get", "configmap", "web-config", "-o", "json"]).stdout
    deployment = run(["kubectl", "-n", "lab3", "get", "deployment", "web", "-o", "json"]).stdout
    result = {
        "checked_at": datetime.now(UTC).isoformat(),
        "passed": not matches and needle not in image and needle not in configmap and needle not in deployment,
        "password_value_redacted": True,
        "tracked_file_matches": matches,
        "repository_file_scope": "tracked plus untracked non-ignored files",
        "password_in_image_inspect": needle in image,
        "password_in_web_configmap": needle in configmap,
        "password_in_deployment_spec": needle in deployment,
        "env_ignored": ".env" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines(),
        "evidence": "The secret value was compared in memory and is not included in this output.",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if not result["passed"] or not result["env_ignored"]:
        raise AssertionError(result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
