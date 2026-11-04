"""Create deterministic gzip copies of the versioned Lab 3 raw runs."""
from __future__ import annotations

import gzip
import hashlib
import json
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "results" / "lab3_final" / "raw"
RUNS = (
    "n1",
    "n2",
    "n3",
    "n5",
    "fixed_profile",
    "hpa30_profile",
    "d5_hpa30",
    "d5_hpa300",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    packaged = []
    for name in RUNS:
        source = RAW / f"{name}.jsonl"
        summary = json.loads(
            (RAW / f"{name}.summary.json").read_text(encoding="utf-8")
        )
        if not source.exists():
            raise FileNotFoundError(source)
        source_hash = sha256(source)
        if source_hash != summary["raw_sha256"]:
            raise RuntimeError(f"SHA-256 mismatch for {source.name}")

        destination = source.with_suffix(source.suffix + ".gz")
        with source.open("rb") as input_stream, destination.open("wb") as output_stream:
            with gzip.GzipFile(
                filename=source.name,
                mode="wb",
                compresslevel=9,
                fileobj=output_stream,
                mtime=0,
            ) as compressed:
                shutil.copyfileobj(input_stream, compressed, length=1024 * 1024)

        restored = hashlib.sha256()
        records = 0
        with gzip.open(destination, "rb") as compressed:
            for line in compressed:
                restored.update(line)
                records += 1
        if restored.hexdigest() != source_hash:
            raise RuntimeError(f"Compressed run does not reproduce {source.name}")
        if records != summary["raw_records"]:
            raise RuntimeError(f"Record count mismatch for {source.name}")
        packaged.append(
            {
                "run": name,
                "records": records,
                "source_bytes": source.stat().st_size,
                "gzip_bytes": destination.stat().st_size,
                "uncompressed_sha256": source_hash,
            }
        )
    print(json.dumps(packaged, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
