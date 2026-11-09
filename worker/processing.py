import math
from typing import Any


def quantile_level(histogram: list[int], quantile: float, total: int) -> int:
    target = max(1, math.ceil(quantile * total))
    cumulative = 0
    for level, count in enumerate(histogram):
        cumulative += count
        if cumulative >= target:
            return level
    return len(histogram) - 1


def process_slice(seed: int, size: int, q_lo: float, q_hi: float) -> dict[str, Any]:
    if seed == 0:
        raise ValueError("Отруєна задача: seed=0")
    count = size * size
    values = [0] * count
    histogram = [0] * 4096
    state = seed
    for index in range(count):
        state = (1103515245 * state + 12345) % (2**31)
        pixel = state >> 19
        values[index] = pixel
        histogram[pixel] += 1

    p_lo = quantile_level(histogram, q_lo, count)
    p_hi = quantile_level(histogram, q_hi, count)
    if p_hi <= p_lo:
        raise ValueError("Квантильне вікно має нульову ширину")

    denominator = p_hi - p_lo
    removed = 0
    kept = 0
    total = 0.0
    total_squares = 0.0
    for pixel in values:
        if pixel < p_lo or pixel > p_hi:
            removed += 1
            continue
        transformed = (4095 * (pixel - p_lo) + denominator // 2) // denominator
        kept += 1
        total += transformed
        total_squares += transformed * transformed
    mean = total / kept
    variance = max(0.0, total_squares / kept - mean * mean)
    return {
        "p_lo": p_lo,
        "p_hi": p_hi,
        "removed_pixels": removed,
        "kept_pixels": kept,
        "mean": mean,
        "stddev": math.sqrt(variance),
    }

