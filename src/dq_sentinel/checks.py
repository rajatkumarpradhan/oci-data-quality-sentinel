"""Compare a candidate CSV with a baseline profile. Findings are review priorities."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from . import drift, expectations, profile

SEV = {"high": 0, "medium": 1, "low": 2}
MIN_ROWS = 50            # below this, drift statistics are not trusted
PSI_MEDIUM, PSI_HIGH = 0.10, 0.25   # common rule-of-thumb cut-offs, not guarantees
KS_HIGH = 0.30
NULL_JUMP_MEDIUM, NULL_JUMP_HIGH = 0.05, 0.20
RANGE_OUT_MEDIUM = 0.03  # share outside baseline [min, max]; same-distribution batches stay around 1% or less


DATE_MIN_ROWS = MIN_ROWS
MIN_BASE_DAYS_GAP = 3      # baseline needs this many distinct days to say what a normal gap is
MIN_BASE_DAYS_WEEKDAY = 14  # two weeks, or the weekday mix says nothing
GAP_MIN_DAYS = 1  # a candidate gap must beat the baseline's largest gap by this much (and by 2x)


def _share_psi(base: list[float], cand: list[float]) -> float:
    import math
    psi = 0.0
    for e, a in zip(base, cand):
        a, e = max(a, drift.EPS), max(e, drift.EPS)
        psi += (a - e) * math.log(a / e)
    return psi


def _noise_adjusted_psi(base: list[float], cand: list[float], n: int, n_base: int) -> float:
    """PSI minus its expected sampling noise for two same-distribution samples: about (bins - 1) * (1/n + 1/n_base)."""
    return _share_psi(base, cand) - (len(base) - 1) * (1 / n + 1 / max(n_base, 1))


@dataclass
class Finding:
    rule: str
    severity: str
    column: str
    message: str
    evidence: dict

    def to_dict(self):
        return asdict(self)


def _date_findings(name: str, b: dict, live: list[str], now: datetime | None) -> list[Finding]:
    out: list[Finding] = []
    parsed = [profile.parse_date(v) for v in live]
    ds = sorted(d for d, _ in parsed)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    # date-only values are compared by calendar day, so "today" is never in the future
    future = [d for d, t in parsed if (d > now if t else d.date() > now.date())]
    if future:
        share = len(future) / len(ds)
        out.append(Finding("DQ014", "high" if share >= 0.05 else "medium", name,
                           f"{len(future)} value(s) ({share:.1%}) are in the future; latest {max(future).isoformat()}.",
                           {"future_count": len(future), "latest": max(future).isoformat()}))
    # future values are reported above and left out of the gap and mix checks, so they cannot fake a gap
    parsed = [(d, t) for d, t in parsed if not (d > now if t else d.date() > now.date())]
    ds = sorted(d for d, _ in parsed)
    days = sorted({d.date() for d in ds})
    gaps = [(b2 - a).days for a, b2 in zip(days, days[1:])]
    if gaps and len(ds) >= DATE_MIN_ROWS and b.get("distinct_days", 0) >= MIN_BASE_DAYS_GAP:
        base_gap = b.get("max_gap_days", 0)
        limit = max(2 * base_gap, base_gap + GAP_MIN_DAYS)
        if max(gaps) > limit:
            out.append(Finding("DQ015", "medium", name,
                               f"Gap of {max(gaps)} days with no records (baseline largest gap {base_gap}, limit {limit}).",
                               {"max_gap_days": max(gaps), "baseline_max_gap_days": base_gap}))
    if len(ds) >= DATE_MIN_ROWS and b.get("distinct_days", 0) >= MIN_BASE_DAYS_WEEKDAY and "weekday_shares" in b:
        cand = [sum(d.weekday() == i for d in ds) / len(ds) for i in range(7)]
        adj = _noise_adjusted_psi(b["weekday_shares"], cand, len(ds), b.get("n", 0))
        if adj >= PSI_MEDIUM:
            out.append(Finding("DQ016", "high" if adj >= PSI_HIGH else "medium", name,
                               f"Day-of-week mix shift (noise-adjusted PSI {adj:.2f}).", {"psi_adjusted": round(adj, 4)}))
    if b.get("has_time") and "hour_shares" in b:
        tm = sorted(d for d, t in parsed if t)
        if len(tm) >= DATE_MIN_ROWS:
            ch = [sum(d.hour == i for d in tm) / len(tm) for i in range(24)]
            adj = _noise_adjusted_psi(b["hour_shares"], ch, len(tm), b.get("n", 0))
            if adj >= PSI_MEDIUM:
                out.append(Finding("DQ017", "high" if adj >= PSI_HIGH else "medium", name,
                                   f"Hour-of-day mix shift (noise-adjusted PSI {adj:.2f}).", {"psi_adjusted": round(adj, 4)}))
    return out


def check(baseline: dict, path: str, key: str | None = None, timestamp_col: str | None = None,
          max_age_days: float | None = None, now: datetime | None = None,
          expect: dict | None = None) -> list[Finding]:
    cols, rows = profile.read_csv(path)
    out: list[Finding] = []
    base = {c["name"]: c for c in baseline["columns"]}
    for name in base:
        if name not in cols:
            out.append(Finding("DQ001", "high", name, "Column present in baseline is missing.", {}))
    for name in cols:
        if name not in base:
            out.append(Finding("DQ002", "medium", name, "New column not in baseline.", {}))
    small = len(rows) < MIN_ROWS
    if small:
        out.append(Finding("DQ009", "low", "*", f"Only {len(rows)} rows (< {MIN_ROWS}); drift statistics skipped.",
                           {"rows": len(rows)}))
    for name in cols:
        if name not in base:
            continue
        b = base[name]
        vals = [r[name] for r in rows]
        cur_type = profile.infer_type(vals)
        if cur_type == "date" and b["type"] == "categorical":
            cur_type = "categorical"  # profile made before date detection: dates were plain text
        if cur_type != b["type"] and cur_type != "empty" and b["type"] != "empty":
            out.append(Finding("DQ003", "high", name, f"Type changed from {b['type']} to {cur_type}.",
                               {"baseline": b["type"], "candidate": cur_type}))
            continue
        nr = sum(profile.is_null(v) for v in vals) / len(vals)
        jump = nr - b["null_rate"]
        if jump >= NULL_JUMP_MEDIUM:
            sev = "high" if jump >= NULL_JUMP_HIGH else "medium"
            out.append(Finding("DQ004", sev, name, f"Null rate rose from {b['null_rate']:.1%} to {nr:.1%}.",
                               {"baseline": b["null_rate"], "candidate": nr}))
        live = [v for v in vals if not profile.is_null(v)]
        if small or not live or b["type"] == "empty":
            continue
        if name == baseline.get("key") or name == key or name in baseline.get("ignore_drift", []):
            continue  # identifiers and declared columns are not drift-tested
        if b["type"] == "date":
            out.extend(_date_findings(name, b, live, now))
            continue
        if b["type"] == "categorical" and b.get("freq_truncated"):
            continue  # high-cardinality text (ids, timestamps, free text): only nulls are checked
        if b["type"] == "numeric":
            xs = [float(v) for v in live]
            psi = drift.psi_numeric(b["deciles"], b["bin_shares"], xs)
            ks = drift.ks_statistic(b["sample"], xs)
            if psi >= PSI_MEDIUM or ks >= KS_HIGH:
                sev = "high" if (psi >= PSI_HIGH or ks >= KS_HIGH) else "medium"
                out.append(Finding("DQ005", sev, name, f"Distribution shift (PSI {psi:.2f}, KS {ks:.2f}).",
                                   {"psi": round(psi, 4), "ks": round(ks, 4)}))
            outside = sum(x < b["min"] or x > b["max"] for x in xs) / len(xs)
            if outside >= RANGE_OUT_MEDIUM:
                out.append(Finding("DQ006", "medium", name,
                                   f"{outside:.1%} of values fall outside the baseline range [{b['min']:g}, {b['max']:g}].",
                                   {"outside_share": round(outside, 4)}))
        else:
            psi = drift.psi_categorical(b["freq"], live)
            new = sorted(set(live) - set(b["freq"])) if not b.get("freq_truncated") else []
            if psi >= PSI_MEDIUM:
                out.append(Finding("DQ007", "high" if psi >= PSI_HIGH else "medium", name,
                                   f"Category mix shift (PSI {psi:.2f})" + (f"; new values: {', '.join(new[:5])}" if new else "") + ".",
                                   {"psi": round(psi, 4), "new_values": new[:10]}))
            elif new:
                out.append(Finding("DQ007", "low", name, f"New category values: {', '.join(new[:5])}.",
                                   {"new_values": new[:10]}))
    k = key or baseline.get("key")
    if k and k in cols:
        vals = [r[k] for r in rows]
        dups = len(vals) - len(set(vals))
        if dups:
            out.append(Finding("DQ008", "high", k, f"{dups} duplicate key value(s) among {len(vals)} rows.",
                               {"duplicates": dups}))
    if timestamp_col and max_age_days is not None:
        if timestamp_col not in cols:
            out.append(Finding("DQ010", "medium", timestamp_col, "Freshness column missing.", {}))
        else:
            now = now or datetime.now(timezone.utc)
            ts = []
            for r in rows:
                try:
                    d = datetime.fromisoformat(r[timestamp_col].replace("Z", "+00:00"))
                    ts.append(d if d.tzinfo else d.replace(tzinfo=timezone.utc))
                except ValueError:
                    pass
            if not ts:
                out.append(Finding("DQ010", "medium", timestamp_col, "No parseable timestamps.", {}))
            else:
                age = (now - max(ts)).total_seconds() / 86400
                if age > max_age_days:
                    out.append(Finding("DQ010", "medium", timestamp_col,
                                       f"Newest record is {age:.1f} days old (limit {max_age_days:g}).",
                                       {"age_days": round(age, 2)}))
    if expect:
        out.extend(expectations.evaluate(expect, cols, rows, Finding))
    out.sort(key=lambda f: (SEV[f.severity], f.rule, f.column))
    return out
