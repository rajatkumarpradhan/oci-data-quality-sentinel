from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone

from . import checks, expectations, profile


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="dq-sentinel", description="Offline CSV profiling and drift checks (synthetic/exported data).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("profile", help="build a baseline profile from a CSV")
    p.add_argument("csv")
    p.add_argument("--key", help="column that must be unique")
    p.add_argument("--ignore-drift", nargs="*", default=[], help="columns excluded from drift tests (ids, sequence numbers)")
    p.add_argument("--output", required=True)
    c = sub.add_parser("check", help="check a CSV against a baseline profile")
    c.add_argument("profile")
    c.add_argument("csv")
    c.add_argument("--key")
    c.add_argument("--timestamp-col")
    c.add_argument("--max-age-days", type=float)
    c.add_argument("--now", help="ISO time to measure freshness against (default: current time)")
    c.add_argument("--expectations", help="JSON file with allowed_values, patterns and cross-column rules")
    c.add_argument("--output")
    c.add_argument("--fail-on", choices=["high", "medium", "low"])
    a = ap.parse_args(argv)
    try:
        if a.cmd == "profile":
            prof = profile.profile_file(a.csv, a.key, a.ignore_drift)
            with open(a.output, "w", encoding="utf-8") as f:
                json.dump(prof, f, indent=1)
            print(f"profiled {prof['rows']} rows, {len(prof['columns'])} columns -> {a.output}")
            return 0
        with open(a.profile, encoding="utf-8") as f:
            base = json.load(f)
        now = datetime.fromisoformat(a.now.replace("Z", "+00:00")) if a.now else None
        if now and not now.tzinfo:
            now = now.replace(tzinfo=timezone.utc)
        expect = None
        if a.expectations:
            with open(a.expectations, encoding="utf-8") as f:
                expect = expectations.load(json.load(f))
        fs = checks.check(base, a.csv, a.key, a.timestamp_col, a.max_age_days, now, expect)
    except (OSError, json.JSONDecodeError, csv.Error, profile.ProfileError, expectations.ExpectationError, ValueError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    for f in fs:
        print(f"[{f.severity.upper():6}] {f.rule} {f.column}: {f.message}")
    print(f"{len(fs)} finding(s).")
    if a.output:
        with open(a.output, "w", encoding="utf-8") as f:
            json.dump({"findings": [x.to_dict() for x in fs]}, f, indent=1)
    if a.fail_on and any(checks.SEV[f.severity] <= checks.SEV[a.fail_on] for f in fs):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
