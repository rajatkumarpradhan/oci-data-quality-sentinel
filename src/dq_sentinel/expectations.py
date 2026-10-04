"""Optional user-defined expectations (JSON): value sets, regex patterns, cross-column rules."""
from __future__ import annotations

import re
from datetime import datetime

from . import profile

OPS = {">=", ">", "<=", "<", "==", "!="}
MAX_SAMPLES = 5


class ExpectationError(ValueError):
    pass


def load(doc: dict) -> dict:
    """Validate an expectations document and return it with compiled patterns."""
    if not isinstance(doc, dict):
        raise ExpectationError("expectations must be a JSON object")
    unknown = set(doc) - {"allowed_values", "patterns", "rules"}
    if unknown:
        raise ExpectationError(f"unknown top-level key(s): {', '.join(sorted(unknown))}")
    out = {"allowed_values": {}, "patterns": {}, "rules": []}
    for col, vals in (doc.get("allowed_values") or {}).items():
        if not isinstance(vals, list) or not vals:
            raise ExpectationError(f"allowed_values.{col} must be a non-empty list")
        out["allowed_values"][col] = {str(v) for v in vals}
    for col, pat in (doc.get("patterns") or {}).items():
        if not isinstance(pat, str):
            raise ExpectationError(f"patterns.{col} must be a string")
        try:
            out["patterns"][col] = re.compile(pat)
        except re.error as e:
            raise ExpectationError(f"patterns.{col}: invalid regex ({e})")
    for i, r in enumerate(doc.get("rules") or []):
        if not isinstance(r, dict) or not {"left", "op"} <= set(r) or ("right" in r) == ("right_value" in r):
            raise ExpectationError(f"rules[{i}] needs left, op and exactly one of right (column) or right_value (number)")
        if "right_value" in r and (isinstance(r["right_value"], bool) or not isinstance(r["right_value"], (int, float))):
            raise ExpectationError(f"rules[{i}]: right_value must be a number")
        if r["op"] not in OPS:
            raise ExpectationError(f"rules[{i}]: op must be one of {', '.join(sorted(OPS))}")
        rhs = r["right"] if "right" in r else r["right_value"]
        out["rules"].append({"left": r["left"], "op": r["op"], "right": r.get("right"), "value": r.get("right_value"),
                             "name": r.get("name") or f"{r['left']} {r['op']} {rhs}"})
    return out


def _cmp(a, op, b) -> bool:
    return {">=": a >= b, ">": a > b, "<=": a <= b, "<": a < b, "==": a == b, "!=": a != b}[op]


def _value(raw: str):
    """Number, else ISO date/time, else text. None for blank."""
    if profile.is_null(raw):
        return None
    try:
        return ("n", float(raw))
    except ValueError:
        pass
    try:
        d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return ("d", d.replace(tzinfo=None) if d.tzinfo else d, bool(d.tzinfo))
    except ValueError:
        return ("s", raw)


def evaluate(exp: dict, cols: list[str], rows: list[dict], make_finding):
    """Yield findings. Blank values are skipped (null checks live elsewhere)."""
    for col, allowed in exp["allowed_values"].items():
        if col not in cols:
            yield make_finding("DQ011", "medium", col, "Column named in allowed_values is missing.", {})
            continue
        bad = [r[col] for r in rows if not profile.is_null(r[col]) and r[col] not in allowed]
        if bad:
            yield make_finding("DQ011", "high", col,
                               f"{len(bad)} value(s) outside the allowed set, e.g. {', '.join(sorted(set(bad))[:MAX_SAMPLES])}.",
                               {"violations": len(bad), "examples": sorted(set(bad))[:MAX_SAMPLES]})
    for col, rx in exp["patterns"].items():
        if col not in cols:
            yield make_finding("DQ012", "medium", col, "Column named in patterns is missing.", {})
            continue
        bad = [r[col] for r in rows if not profile.is_null(r[col]) and not rx.fullmatch(r[col])]
        if bad:
            yield make_finding("DQ012", "medium", col,
                               f"{len(bad)} value(s) do not fully match /{rx.pattern}/, e.g. {', '.join(bad[:MAX_SAMPLES])}.",
                               {"violations": len(bad), "examples": bad[:MAX_SAMPLES]})
    for rule in exp["rules"]:
        l, r_ = rule["left"], rule["right"]
        missing = [c for c in (l, r_) if c is not None and c not in cols]
        if missing:
            yield make_finding("DQ013", "medium", l, f"Rule '{rule['name']}' names missing column(s): {', '.join(missing)}.", {})
            continue
        bad = incomparable = 0
        for row in rows:
            a = _value(row[l])
            b = ("n", float(rule["value"])) if r_ is None else _value(row[r_])
            if a is None or b is None:
                continue
            if a[0] != b[0] or (a[0] == "d" and a[2] != b[2]):
                incomparable += 1
                continue
            if not _cmp(a[1], rule["op"], b[1]):
                bad += 1
        if bad:
            yield make_finding("DQ013", "high", l, f"Rule '{rule['name']}' fails on {bad} row(s).",
                               {"violations": bad, "rule": rule["name"]})
        if incomparable:
            yield make_finding("DQ013", "low", l,
                               f"Rule '{rule['name']}': {incomparable} row(s) skipped because the two values are different kinds (number, date, text) or mix timezone-aware and naive dates.",
                               {"skipped": incomparable, "rule": rule["name"]})
