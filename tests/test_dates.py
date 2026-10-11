import csv
import os
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from dq_sentinel import checks, profile
from dq_sentinel.cli import main

HERE = os.path.dirname(__file__)
EX = os.path.join(HERE, "..", "examples")
NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)


def write_csv(rows, header=("id", "ts")):
    f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="", encoding="utf-8")
    w = csv.writer(f)
    w.writerow(header)
    w.writerows(rows)
    f.close()
    return f.name


def col(path, name="ts"):
    return next(c for c in profile.profile_file(path)["columns"] if c["name"] == name)


def weekday_rows(start, days, per, hours, seed, weekdays_only=True, id0=0):
    r = random.Random(seed)
    out = []
    for d in range(days):
        day = start + timedelta(days=d)
        if weekdays_only and day.weekday() >= 5:
            continue
        for _ in range(per):
            out.append([id0 + len(out), day.replace(hour=r.choice(hours), minute=r.randrange(60)).strftime("%Y-%m-%dT%H:%M:%SZ")])
    return out


MON = datetime(2026, 8, 3)
HOURS = list(range(8, 18))


def base_profile(seed=1):
    return profile.profile_file(write_csv(weekday_rows(MON, 28, 12, HOURS, seed)), key="id")


def run(rows, seed_profile=None, now=NOW):
    return checks.check(seed_profile or base_profile(), write_csv(rows), now=now)


def rules(fs):
    return sorted(f.rule for f in fs if f.column == "ts")


class ParseTests(unittest.TestCase):
    def test_accepted_formats(self):
        for v in ("2026-09-01", "2026-09-01T10:30", "2026-09-01 10:30:15", "2026-09-01T10:30:15Z",
                  "2026-09-01T10:30:15.250Z", "2026-09-01T10:30:15+05:30", "2026-09-01T10:30:15-0700"):
            with self.subTest(v=v):
                self.assertIsNotNone(profile.parse_date(v))

    def test_rejected_formats(self):
        for v in ("13/05/2026", "2026-9-1", "20260901", "2026-13-01", "2026-02-30", "yesterday", "2026", "2026-09-01T25:00:00", "1.5"):
            with self.subTest(v=v):
                self.assertIsNone(profile.parse_date(v))

    def test_offsets_are_converted_to_utc_and_date_only_is_midnight(self):
        d, t = profile.parse_date("2026-09-01T00:30:00+05:30")
        self.assertEqual(d, datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc))
        self.assertTrue(t)
        d, t = profile.parse_date("2026-09-01")
        self.assertEqual((d.hour, t), (0, False))


class DetectionTests(unittest.TestCase):
    def test_iso_dates_detected_without_time(self):
        c = col(write_csv([[i, f"2026-09-{i % 28 + 1:02d}"] for i in range(60)]))
        self.assertEqual(c["type"], "date")
        self.assertFalse(c["has_time"])
        self.assertNotIn("hour_shares", c)

    def test_timestamps_detected_with_hour_shares(self):
        c = col(write_csv([[i, f"2026-09-01T{i % 24:02d}:00:00Z"] for i in range(48)]))
        self.assertEqual(c["type"], "date")
        self.assertTrue(c["has_time"])
        self.assertAlmostEqual(sum(c["hour_shares"]), 1.0)
        self.assertAlmostEqual(sum(c["weekday_shares"]), 1.0)

    def test_mixed_date_and_timestamp_is_still_a_date(self):
        self.assertEqual(col(write_csv([[0, "2026-09-01"], [1, "2026-09-02T10:00:00Z"]]))["type"], "date")

    def test_mixed_formats_stay_categorical(self):
        self.assertEqual(col(write_csv([[0, "2026-09-01"], [1, "02/09/2026"]]))["type"], "categorical")
        self.assertEqual(col(write_csv([[0, "2026-09-01"], [1, "n/a-ish"]]))["type"], "categorical")

    def test_numbers_are_not_dates_and_nulls_are_ignored(self):
        self.assertEqual(col(write_csv([[0, "20260901"], [1, "20260902"]]))["type"], "numeric")
        self.assertEqual(col(write_csv([[0, "2026-09-01"], [1, ""], [2, "NA"]]))["type"], "date")
        self.assertEqual(col(write_csv([[0, ""], [1, "null"]]))["type"], "empty")

    def test_profile_stats(self):
        c = col(write_csv([[0, "2026-09-01"], [1, "2026-09-02"], [2, "2026-09-06"]]))
        self.assertEqual(c["distinct_days"], 3)
        self.assertEqual(c["max_gap_days"], 4)
        self.assertTrue(c["min"].startswith("2026-09-01"))
        self.assertTrue(c["max"].startswith("2026-09-06"))

    def test_baseline_with_one_day_has_zero_gap(self):
        self.assertEqual(col(write_csv([[0, "2026-09-01"]]))["max_gap_days"], 0)


class CheckTests(unittest.TestCase):
    def test_same_pattern_new_period_is_clean(self):
        for seed in range(2, 12):
            with self.subTest(seed=seed):
                self.assertEqual(rules(run(weekday_rows(datetime(2026, 9, 7), 14, 12, HOURS, seed))), [])

    def test_no_false_positives_across_many_same_distribution_batches(self):
        bad = 0
        for seed in range(100, 140):
            fs = run(weekday_rows(datetime(2026, 9, 7), 14, 12, HOURS, seed), base_profile(seed=seed + 1000))
            bad += bool(rules(fs))
        self.assertEqual(bad, 0)

    def test_future_dates_flagged_with_severity_split(self):
        rows = weekday_rows(datetime(2026, 9, 7), 14, 12, HOURS, 3)
        rows[0][1] = "2027-01-01T10:00:00Z"
        f = [x for x in run(rows) if x.rule == "DQ014"]
        self.assertEqual((len(f), f[0].severity), (1, "medium"))
        for r in rows[:20]:
            r[1] = "2027-01-01T10:00:00Z"
        f = [x for x in run(rows) if x.rule == "DQ014"]
        self.assertEqual(f[0].severity, "high")

    def test_future_boundary_now_itself_and_date_only_today(self):
        rows = weekday_rows(datetime(2026, 9, 7), 14, 12, HOURS, 3)
        rows[0][1] = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertNotIn("DQ014", rules(run(rows)))
        rows[0][1] = (NOW + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertIn("DQ014", rules(run(rows)))
        dprof = profile.profile_file(write_csv([[i, f"2026-09-{i % 28 + 1:02d}"] for i in range(60)]))
        same = checks.check(dprof, write_csv([[i, "2026-10-12"] for i in range(60)]), now=NOW)
        self.assertNotIn("DQ014", rules(same))  # a date-only value for today is not in the future
        nxt = checks.check(dprof, write_csv([[i, "2026-10-13"] for i in range(60)]), now=NOW)
        self.assertIn("DQ014", rules(nxt))

    def test_gap_boundary(self):
        base = base_profile()  # weekday data: largest gap is 3 days (Fri -> Mon), limit 6
        def with_hole(hole_days):
            rows = []
            day = datetime(2026, 9, 7)
            for d in range(40):
                cur = day + timedelta(days=d)
                if cur.weekday() >= 5 or (3 <= d < 3 + hole_days):
                    continue
                rows += [[len(rows) + i, cur.replace(hour=9 + i % 8).strftime("%Y-%m-%dT%H:%M:%SZ")] for i in range(5)]
            return rows
        self.assertNotIn("DQ015", rules(run(with_hole(0))))
        self.assertNotIn("DQ015", rules(run(with_hole(2))))  # normal weekend plus a short hole stays under the limit
        self.assertIn("DQ015", rules(run(with_hole(8))))

    def test_weekend_shift_flagged(self):
        rows = weekday_rows(datetime(2026, 9, 7), 28, 6, HOURS, 4, weekdays_only=False)
        self.assertIn("DQ016", rules(run(rows)))

    def test_hour_shift_flagged_and_names_the_right_rule(self):
        rows = weekday_rows(datetime(2026, 9, 7), 14, 12, [0, 1, 2, 3, 22, 23], 5)
        r = rules(run(rows))
        self.assertIn("DQ017", r)
        self.assertNotIn("DQ016", r)

    def test_future_rows_do_not_fake_a_gap(self):
        rows = weekday_rows(datetime(2026, 9, 7), 14, 12, HOURS, 6)
        rows.append([9999, "2027-06-01T10:00:00Z"])
        r = rules(run(rows))
        self.assertIn("DQ014", r)
        self.assertNotIn("DQ015", r)

    def test_small_batch_skips_date_mix_checks(self):
        rows = weekday_rows(datetime(2026, 9, 7), 2, 5, [0, 1], 7)  # 10 rows
        self.assertEqual([x for x in rules(run(rows)) if x != "DQ009"], [])

    def test_short_baseline_gives_no_weekday_or_gap_findings(self):
        short = profile.profile_file(write_csv(weekday_rows(MON, 2, 30, HOURS, 8)))
        rows = weekday_rows(datetime(2026, 9, 7), 28, 6, HOURS, 9, weekdays_only=False)
        r = rules(run(rows, short))
        self.assertNotIn("DQ016", r)
        self.assertNotIn("DQ015", r)

    def test_date_column_turning_into_text_is_a_type_change(self):
        rows = [[i, "n/a-ish" if i % 2 else "2026-09-01"] for i in range(80)]
        self.assertIn("DQ003", rules(run(rows)))

    def test_old_profile_with_text_dates_is_not_a_type_change(self):
        legacy = base_profile()
        for c in legacy["columns"]:
            if c["name"] == "ts":
                c.clear()
                c.update({"name": "ts", "type": "categorical", "rows": 100, "null_rate": 0.0, "distinct": 90,
                          "freq": {}, "freq_truncated": True})
        self.assertEqual(rules(run(weekday_rows(datetime(2026, 9, 7), 14, 12, HOURS, 3), legacy)), [])

    def test_null_rate_still_checked_on_date_columns(self):
        rows = weekday_rows(datetime(2026, 9, 7), 14, 12, HOURS, 3)
        for r in rows[::2]:
            r[1] = ""
        self.assertIn("DQ004", rules(run(rows)))


class ExampleAndCliTests(unittest.TestCase):
    def run_cli(self, argv):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(argv)
        return code, out.getvalue()

    def test_example_files(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "p.json")
            self.assertEqual(self.run_cli(["profile", os.path.join(EX, "events_baseline.csv"), "--key", "event_id", "--output", p])[0], 0)
            code, out = self.run_cli(["check", p, os.path.join(EX, "events_good.csv"), "--now", "2026-10-12T00:00:00Z"])
            self.assertEqual((code, "0 finding(s)" in out), (0, True))
            code, out = self.run_cli(["check", p, os.path.join(EX, "events_drifted.csv"), "--now", "2026-10-12T00:00:00Z", "--fail-on", "high"])
            self.assertEqual(code, 2)
            for rule in ("DQ014", "DQ015", "DQ016", "DQ017"):
                self.assertIn(rule, out)


if __name__ == "__main__":
    unittest.main()
