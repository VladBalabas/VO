"""Create deterministic gzip copies of the four versioned Lab 2 raw profiles."""
from __future__ import annotations

import gzip
import hashlib
import json
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "results" / "lab2_final" / "raw"
PROFILES = tuple(f"n{n}_profile.json" for n in range(1, 5))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    packaged = []
    for name in PROFILES:
        source = RAW / name
        if not source.exists():
            raise FileNotFoundError(source)
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
        with gzip.open(destination, "rb") as compressed:
            restored = hashlib.sha256()
            for chunk in iter(lambda: compressed.read(1024 * 1024), b""):
                restored.update(chunk)
        source_hash = sha256(source)
        if restored.hexdigest() != source_hash:
            raise RuntimeError(f"Compressed profile does not reproduce {name}")
        packaged.append(
            {
                "profile": name,
                "source_bytes": source.stat().st_size,
                "gzip_bytes": destination.stat().st_size,
                "uncompressed_sha256": source_hash,
            }
        )
    print(json.dumps(packaged, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
