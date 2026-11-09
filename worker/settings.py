import os
from dataclasses import dataclass


def required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Не задано {name}")
    return value


def integer(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as error:
        raise RuntimeError(f"{name} має бути цілим числом") from error
    if value < minimum:
        raise RuntimeError(f"{name} має бути не меншим за {minimum}")
    return value


def number(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as error:
        raise RuntimeError(f"{name} має бути числом") from error
    if value < minimum:
        raise RuntimeError(f"{name} має бути не меншим за {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    postgres_host: str
    postgres_port: int
    postgres_database: str
    postgres_user: str
    postgres_password: str
    web_url: str
    worker_id: str
    lease_seconds: float
    poll_seconds: float
    fail_rate: float
    claim_mode: str
    claim_batch_size: int
    idempotent_results: bool
    slice_size: int = 256
    q_lo: float = 0.05
    q_hi: float = 0.95
    metrics_port: int = 9100


def load_settings() -> WorkerSettings:
    mode = os.getenv("CLAIM_MODE", "skip_locked")
    if mode not in {"naive", "skip_locked", "locked"}:
        raise RuntimeError("CLAIM_MODE має бути naive, skip_locked або locked")
    fail_rate = number("FAIL_RATE", 0.0)
    if fail_rate > 1:
        raise RuntimeError("FAIL_RATE має бути від 0 до 1")
    q_lo = number("Q_LO", 0.05)
    q_hi = number("Q_HI", 0.95)
    if not 0 <= q_lo < q_hi <= 1:
        raise RuntimeError("Квантилі мають задовольняти 0 <= Q_LO < Q_HI <= 1")
    return WorkerSettings(
        postgres_host=required("POSTGRES_HOST"),
        postgres_port=integer("POSTGRES_PORT", 5432),
        postgres_database=required("POSTGRES_DB"),
        postgres_user=required("POSTGRES_USER"),
        postgres_password=required("POSTGRES_PASSWORD"),
        web_url=required("WEB_URL").rstrip("/"),
        worker_id=required("WORKER_ID"),
        lease_seconds=number("LEASE_SECONDS", 5.0, 0.1),
        poll_seconds=number("POLL_INTERVAL_MS", 100.0) / 1000,
        fail_rate=fail_rate,
        claim_mode=mode,
        claim_batch_size=integer("CLAIM_BATCH_SIZE", 1),
        idempotent_results=os.getenv("IDEMPOTENT_RESULTS", "true").lower()
        not in {"0", "false", "no"},
        slice_size=integer("SLICE_SIZE", 256),
        q_lo=q_lo,
        q_hi=q_hi,
        metrics_port=integer("METRICS_PORT", 9100),
    )

