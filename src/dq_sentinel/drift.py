"""Drift statistics. PSI uses baseline deciles; KS is the two-sample statistic."""
from __future__ import annotations

import bisect
import math

EPS = 1e-4


def bin_index(edges: list[float], x: float) -> int:
    return bisect.bisect_left(edges, x)


def bin_shares(edges: list[float], values: list[float]) -> list[float]:
    counts = [0] * (len(edges) + 1)
    for x in values:
        counts[bin_index(edges, x)] += 1
    return [c / len(values) for c in counts]


def psi_numeric(edges: list[float], base_shares: list[float], candidate: list[float]) -> float:
    """PSI of `candidate` against the baseline's own binned shares (bins from baseline deciles).

    Using the baseline's measured shares (not an assumed 10% per bin) keeps the statistic
    honest when the baseline has many tied values.
    """
    if not candidate:
        raise ValueError("empty candidate")
    if len(base_shares) != len(edges) + 1:
        raise ValueError("shares do not match edges")
    cand = bin_shares(edges, candidate)
    psi = 0.0
    for a, e in zip(cand, base_shares):
        a, e = max(a, EPS), max(e, EPS)
        psi += (a - e) * math.log(a / e)
    return psi


def psi_categorical(base_freq: dict[str, float], candidate: list[str]) -> float:
    """PSI over baseline categories plus an 'other' bucket."""
    if not candidate:
        raise ValueError("empty candidate")
    n = len(candidate)
    counts: dict[str, int] = {}
    for v in candidate:
        counts[v] = counts.get(v, 0) + 1
    other_base = max(0.0, 1.0 - sum(base_freq.values()))
    psi = 0.0
    for k, e in base_freq.items():
        a = max(counts.get(k, 0) / n, EPS)
        e = max(e, EPS)
        psi += (a - e) * math.log(a / e)
    other_cand = sum(c for k, c in counts.items() if k not in base_freq) / n
    a, e = max(other_cand, EPS), max(other_base, EPS)
    psi += (a - e) * math.log(a / e)
    return psi


def ks_statistic(a: list[float], b: list[float]) -> float:
    if not a or not b:
        raise ValueError("empty sample")
    a, b = sorted(a), sorted(b)
    i = j = 0
    d = 0.0
    while i < len(a) and j < len(b):
        x = min(a[i], b[j])
        while i < len(a) and a[i] <= x:
            i += 1
        while j < len(b) and b[j] <= x:
            j += 1
        d = max(d, abs(i / len(a) - j / len(b)))
    return d
