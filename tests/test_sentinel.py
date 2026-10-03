import csv
import json
import os
import random
import tempfile
import unittest
from datetime import datetime, timezone

from dq_sentinel import checks, drift, profile
from dq_sentinel.cli import main

HERE = os.path.dirname(__file__)
EX = os.path.join(HERE, "..", "examples")
BASE = os.path.join(EX, "baseline.csv")


def write_csv(header, rows):
    f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="", encoding="utf-8")
    w = csv.writer(f)
    w.writerow(header)
    w.writerows(rows)
    f.close()
    return f.name


def gauss_rows(n, mu, sd, seed, cat=("a", "b"), cw=(0.5, 0.5), start=0):
    r = random.Random(seed)
    return [[start + i, round(r.gauss(mu, sd), 3), r.choices(cat, cw)[0]] for i in range(n)]


H = ["id", "x", "c"]


def baseline_profile(seed=1, n=600):
    return profile.profile_file(write_csv(H, gauss_rows(n, 50, 10, seed)), key="id")


def rules(fs):
    return {(f.rule, f.column) for f in fs}


class StatsTests(unittest.TestCase):
    def test_quantile(self):
        self.assertEqual(profile.quantile([1, 2, 3, 4, 5], 0.5), 3)
        self.assertEqual(profile.quantile([0, 10], 0.25), 2.5)

    def test_psi_identical_is_zero(self):
        xs = [float(i) for i in range(1000)]
        edges = [profile.quantile(xs, i / 10) for i in range(1, 10)]
        sh = drift.bin_shares(edges, xs)
        self.assertAlmostEqual(drift.psi_numeric(edges, sh, xs), 0.0, places=6)

    def test_psi_grows_with_shift(self):
        xs = [float(i) for i in range(1000)]
        edges = [profile.quantile(xs, i / 10) for i in range(1, 10)]
        sh = drift.bin_shares(edges, xs)
        small = drift.psi_numeric(edges, sh, [x + 50 for x in xs])
        big = drift.psi_numeric(edges, sh, [x + 400 for x in xs])
        self.assertLess(small, big)
        self.assertGreater(big, 0.25)

    def test_psi_handles_tied_baseline(self):
        xs = [1.0] * 800 + [2.0] * 200
        edges = [profile.quantile(sorted(xs), i / 10) for i in range(1, 10)]
        sh = drift.bin_shares(edges, xs)
        self.assertAlmostEqual(sum(sh), 1.0)
        self.assertAlmostEqual(drift.psi_numeric(edges, sh, xs), 0.0, places=6)

    def test_ks(self):
        self.assertEqual(drift.ks_statistic([1, 2, 3], [1, 2, 3]), 0.0)
        self.assertEqual(drift.ks_statistic([1, 2, 3], [10, 11, 12]), 1.0)
        with self.assertRaises(ValueError):
            drift.ks_statistic([], [1])

    def test_psi_categorical(self):
        base = {"a": 0.5, "b": 0.5}
        self.assertAlmostEqual(drift.psi_categorical(base, ["a", "b"] * 50), 0.0, places=6)
        self.assertGreater(drift.psi_categorical(base, ["c"] * 100), 1.0)


class ProfileTests(unittest.TestCase):
    def test_types_and_nulls(self):
        p = profile.profile_file(write_csv(["a", "b", "c"], [[1, "x", ""], [2, "y", ""], ["NA", "x", ""]]))
        t = {c["name"]: c for c in p["columns"]}
        self.assertEqual(t["a"]["type"], "numeric")
        self.assertAlmostEqual(t["a"]["null_rate"], 1 / 3)
        self.assertEqual(t["b"]["type"], "categorical")
        self.assertEqual(t["c"]["type"], "empty")

    def test_errors(self):
        with self.assertRaises(profile.ProfileError):
            profile.profile_file(write_csv(["a", "a"], [[1, 2]]))
        with self.assertRaises(profile.ProfileError):
            profile.profile_file(write_csv(["a"], []))
        with self.assertRaises(profile.ProfileError):
            profile.profile_file(write_csv(["a"], [[1]]), key="zzz")
        with self.assertRaises(profile.ProfileError):
            profile.profile_file(write_csv(["a"], [[1]]), ignore_drift=["zzz"])

    def test_ragged_row(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
        f.write("a,b\n1,2\n3\n")
        f.close()
        with self.assertRaises(profile.ProfileError):
            profile.read_csv(f.name)

    def test_high_cardinality_truncated(self):
        p = profile.profile_file(write_csv(["s"], [[f"v{i}"] for i in range(100)]))
        self.assertTrue(p["columns"][0]["freq_truncated"])


class CheckTests(unittest.TestCase):
    def check(self, rows, header=H, base=None, **kw):
        return checks.check(base or baseline_profile(), write_csv(header, rows), **kw)

    def test_same_distribution_is_clean_across_seeds(self):
        bad = 0
        for seed in range(20):
            fs = self.check(gauss_rows(300, 50, 10, 100 + seed, start=10_000))
            bad += bool(rules(fs) - set())
        self.assertEqual(bad, 0, "too many false positives on identical distributions")

    def test_mean_shift_flagged(self):
        fs = self.check(gauss_rows(300, 75, 10, 3, start=10_000))
        self.assertIn(("DQ005", "x"), rules(fs))

    def test_missing_and_new_column(self):
        fs = self.check([[1, "a"]] * 60, header=["id", "c"])
        self.assertIn(("DQ001", "x"), rules(fs))
        fs = self.check([[1, 1.0, "a", 9]] * 60, header=["id", "x", "c", "new"])
        self.assertIn(("DQ002", "new"), rules(fs))

    def test_type_change(self):
        rows = [[i, "oops", "a"] for i in range(80)]
        self.assertIn(("DQ003", "x"), rules(self.check(rows)))

    def test_null_jump_severity(self):
        rows = gauss_rows(200, 50, 10, 4, start=10_000)
        for r in rows[:50]:
            r[1] = ""
        fs = [f for f in self.check(rows) if f.rule == "DQ004"]
        self.assertEqual(fs[0].severity, "high")

    def test_new_category_and_shift(self):
        fs = self.check(gauss_rows(300, 50, 10, 5, cat=("a", "z"), cw=(0.2, 0.8), start=10_000))
        f = next(f for f in fs if f.rule == "DQ007")
        self.assertIn("z", f.evidence["new_values"])

    def test_range_violation(self):
        rows = gauss_rows(300, 50, 10, 6, start=10_000)
        for r in rows[:10]:
            r[1] = 9999
        self.assertIn(("DQ006", "x"), rules(self.check(rows)))

    def test_duplicate_keys(self):
        rows = gauss_rows(100, 50, 10, 7, start=10_000)
        rows.append(list(rows[0]))
        fs = self.check(rows)
        self.assertEqual(next(f for f in fs if f.rule == "DQ008").evidence["duplicates"], 1)

    def test_small_batch_skips_drift(self):
        fs = self.check(gauss_rows(10, 500, 1, 8, start=10_000))
        self.assertIn(("DQ009", "*"), rules(fs))
        self.assertNotIn(("DQ005", "x"), rules(fs))

    def test_key_not_drift_tested(self):
        fs = self.check(gauss_rows(300, 50, 10, 9, start=1_000_000))
        self.assertNotIn(("DQ005", "id"), rules(fs))

    def test_ignore_drift_column(self):
        p = profile.profile_file(write_csv(H, gauss_rows(600, 50, 10, 1)), key="id", ignore_drift=["x"])
        fs = checks.check(p, write_csv(H, gauss_rows(300, 90, 10, 2, start=10_000)))
        self.assertNotIn(("DQ005", "x"), rules(fs))

    def test_freshness(self):
        hdr = ["id", "ts"]
        p = profile.profile_file(write_csv(hdr, [[i, "2026-09-01T00:00:00Z"] for i in range(60)]), key="id")
        now = datetime(2026, 9, 20, tzinfo=timezone.utc)
        path = write_csv(hdr, [[100 + i, "2026-09-02T00:00:00Z"] for i in range(60)])
        fs = checks.check(p, path, timestamp_col="ts", max_age_days=7, now=now)
        self.assertIn(("DQ010", "ts"), rules(fs))
        fs = checks.check(p, path, timestamp_col="ts", max_age_days=30, now=now)
        self.assertNotIn(("DQ010", "ts"), rules(fs))

    def test_sorted_by_severity(self):
        fs = self.check(gauss_rows(300, 75, 10, 3, cat=("a", "z"), cw=(0.2, 0.8), start=10_000))
        sev = [checks.SEV[f.severity] for f in fs]
        self.assertEqual(sev, sorted(sev))


class ExampleAndCliTests(unittest.TestCase):
    def setUp(self):
        self.prof = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        self.assertEqual(main(["profile", BASE, "--key", "order_id", "--output", self.prof]), 0)

    def test_good_batch_clean(self):
        self.assertEqual(main(["check", self.prof, os.path.join(EX, "good.csv"), "--fail-on", "low"]), 0)

    def test_drifted_batch_flagged(self):
        out = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        rc = main(["check", self.prof, os.path.join(EX, "drifted.csv"), "--fail-on", "high", "--output", out])
        self.assertEqual(rc, 2)
        with open(out, encoding="utf-8") as f:
            found = {(x["rule"], x["column"]) for x in json.load(f)["findings"]}
        for k in [("DQ004", "amount"), ("DQ005", "latency_ms"), ("DQ007", "region"), ("DQ008", "order_id")]:
            self.assertIn(k, found)

    def test_bad_input(self):
        self.assertEqual(main(["check", self.prof, "/nope.csv"]), 1)
        self.assertEqual(main(["check", "/nope.json", BASE]), 1)


if __name__ == "__main__":
    unittest.main()
