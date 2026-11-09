"""Створює детерміновані gzip-копії первинних JSONL ЛР4."""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "results" / "lab4_final"


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            value.update(block)
    return value.hexdigest()


def main() -> None:
    files = []
    run_root = DATA / "runs"
    logical_sources = set(run_root.glob("*/*.jsonl"))
    logical_sources.update(
        Path(str(archive)[:-3]) for archive in run_root.glob("*/*.jsonl.gz")
    )
    for source in sorted(logical_sources):
        destination = source.with_suffix(source.suffix + ".gz")
        if source.exists():
            with source.open("rb") as input_stream, destination.open("wb") as raw_output:
                with gzip.GzipFile(filename="", mode="wb", fileobj=raw_output, mtime=0) as output:
                    while block := input_stream.read(1024 * 1024):
                        output.write(block)
        elif not destination.exists():
            raise FileNotFoundError(source)
        restored = hashlib.sha256(); uncompressed_bytes = 0; records = 0
        with gzip.open(destination, "rb") as stream:
            while block := stream.read(1024 * 1024):
                restored.update(block)
                uncompressed_bytes += len(block)
                records += block.count(b"\n")
        source_hash = digest(source) if source.exists() else restored.hexdigest()
        if restored.hexdigest() != source_hash:
            raise RuntimeError(f"SHA-256 не збігається для {source}")
        files.append({"source": source.relative_to(DATA).as_posix(),
                      "archive": destination.relative_to(DATA).as_posix(),
                      "bytes": source.stat().st_size if source.exists() else uncompressed_bytes,
                      "archive_bytes": destination.stat().st_size,
                      "sha256": source_hash,
                      "records": records})
    manifest = {"format": "JSON Lines compressed with deterministic gzip mtime=0",
                "files": files,
                "total_records": sum(row["records"] for row in files)}
    (DATA / "raw_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"files": len(files), "records": manifest["total_records"]}, indent=2))


if __name__ == "__main__":
    main()

