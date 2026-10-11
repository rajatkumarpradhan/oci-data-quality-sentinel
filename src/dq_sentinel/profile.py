"""Profile a CSV: per-column type, null rate, numeric deciles, categorical frequencies."""
from __future__ import annotations

import csv
import math
import re
from datetime import datetime, timedelta, timezone

MAX_CATEGORIES = 20
NULL_TOKENS = {"", "na", "n/a", "null", "none", "nan"}


class ProfileError(ValueError):
    pass


def is_null(v: str | None) -> bool:
    return v is None or v.strip().lower() in NULL_TOKENS


def _num(v: str):
    try:
        x = float(v)
    except ValueError:
        return None
    return x if math.isfinite(x) else None


_DATE_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})"
    r"(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?(Z|[+-]\d{2}:?\d{2})?)?$")


def parse_date(v: str):
    """Strict ISO 8601 date or timestamp -> (UTC datetime, has_time), or None.

    Accepts 2026-09-01, 2026-09-01T10:30, 2026-09-01 10:30:15, 2026-09-01T10:30:15Z and
    offsets such as +05:30 or -0700. A date alone is read as midnight UTC. Timestamps without
    an offset are read as UTC. Anything else (13/05/2026, 2026-9-1, 20260901) is not a date.
    Parsed by hand so Python 3.10 and 3.12 agree.
    """
    m = _DATE_RE.match(v.strip())
    if not m:
        return None
    y, mo, d, hh, mi, ss, frac, tz = m.groups()
    has_time = hh is not None
    try:
        dt = datetime(int(y), int(mo), int(d), int(hh or 0), int(mi or 0), int(ss or 0),
                      int((frac or "0").ljust(6, "0")[:6]), tzinfo=timezone.utc)
        if tz and tz != "Z":
            sign = 1 if tz[0] == "+" else -1
            digits = tz[1:].replace(":", "")
            oh, om = int(digits[:2]), int(digits[2:])
            if oh > 23 or om > 59:
                return None
            dt = dt - sign * timedelta(hours=oh, minutes=om)
    except ValueError:
        return None
    return dt, has_time


def read_csv(path: str) -> tuple[list[str], list[dict]]:
    with open(path, newline="", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        if not rd.fieldnames:
            raise ProfileError("CSV has no header")
        if len(set(rd.fieldnames)) != len(rd.fieldnames):
            raise ProfileError("duplicate column names")
        rows = []
        for n, r in enumerate(rd, start=2):
            if None in r or any(v is None for v in r.values()):
                raise ProfileError(f"line {n}: wrong number of fields")
            rows.append(r)
    if not rows:
        raise ProfileError("CSV has no rows")
    return list(rd.fieldnames), rows


def quantile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated quantile, q in [0, 1]."""
    if not sorted_vals:
        raise ValueError("empty")
    pos = q * (len(sorted_vals) - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def infer_type(values: list[str]) -> str:
    vals = [v for v in values if not is_null(v)]
    if not vals:
        return "empty"
    if all(_num(v) is not None for v in vals):
        return "numeric"
    if all(parse_date(v) for v in vals):
        return "date"  # all values are ISO dates or timestamps; mixed formats stay categorical
    return "categorical"


def profile_column(name: str, values: list[str]) -> dict:
    n = len(values)
    nulls = sum(is_null(v) for v in values)
    t = infer_type(values)
    p = {"name": name, "type": t, "rows": n, "null_rate": nulls / n}
    live = [v for v in values if not is_null(v)]
    if t == "numeric":
        xs = sorted(float(v) for v in live)
        from .drift import bin_shares
        deciles = [quantile(xs, i / 10) for i in range(1, 10)]
        p.update(min=xs[0], max=xs[-1], mean=sum(xs) / len(xs), deciles=deciles,
                 bin_shares=bin_shares(deciles, xs),
                 sample=xs if len(xs) <= 2000 else [quantile(xs, i / 1999) for i in range(2000)])
    elif t == "date":
        p.update(date_profile(live))
    elif t == "categorical":
        counts: dict[str, int] = {}
        for v in live:
            counts[v] = counts.get(v, 0) + 1
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_CATEGORIES]
        p["distinct"] = len(counts)
        p["freq"] = {k: c / len(live) for k, c in top}
        p["freq_truncated"] = len(counts) > MAX_CATEGORIES
    return p


def date_profile(live: list[str]) -> dict:
    parsed = [parse_date(v) for v in live]
    ds = sorted(d for d, _ in parsed)
    has_time = any(t for _, t in parsed)
    days = sorted({d.date() for d in ds})
    gaps = [(b - a).days for a, b in zip(days, days[1:])]
    out = {"n": len(ds), "min": ds[0].isoformat(), "max": ds[-1].isoformat(), "has_time": has_time,
           "distinct_days": len(days), "max_gap_days": max(gaps) if gaps else 0,
           "weekday_shares": [sum(d.weekday() == i for d in ds) / len(ds) for i in range(7)]}
    if has_time:
        out["hour_shares"] = [sum(d.hour == i for d in ds) / len(ds) for i in range(24)]
    return out


def profile_file(path: str, key: str | None = None, ignore_drift: list[str] | None = None) -> dict:
    cols, rows = read_csv(path)
    prof = {"rows": len(rows), "columns": [profile_column(c, [r[c] for r in rows]) for c in cols]}
    if key:
        if key not in cols:
            raise ProfileError(f"key column {key!r} not in CSV")
        prof["key"] = key
    if ignore_drift:
        bad = [c for c in ignore_drift if c not in cols]
        if bad:
            raise ProfileError(f"ignore-drift columns not in CSV: {', '.join(bad)}")
        prof["ignore_drift"] = list(ignore_drift)
    return prof
