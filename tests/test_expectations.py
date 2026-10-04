import json
import os
import unittest

from test_sentinel import write_csv, BASE, EX
from dq_sentinel import checks, expectations, profile
from dq_sentinel.cli import main

EXP = os.path.join(EX, "expectations.json")


def run(header, rows, doc):
    path = write_csv(header, rows)
    base = profile.profile_file(BASE, "order_id")
    return checks.check(base, path, expect=expectations.load(doc))


def rules(fs):
    return [(f.rule, f.severity) for f in fs if f.rule in ("DQ011", "DQ012", "DQ013")]


H = ["order_id", "region", "amount", "latency_ms", "created_at", "coupon"]


def row(i, region="ap-mumbai-1", amount="10", lat="5", coupon="SAVE10"):
    return [str(i), region, amount, lat, "2026-09-01T00:00:00Z", coupon]


class LoaderTests(unittest.TestCase):
    def bad(self, doc):
        with self.assertRaises(expectations.ExpectationError):
            expectations.load(doc)

    def test_not_object(self):
        self.bad([])

    def test_unknown_key(self):
        self.bad({"allowed": {}})

    def test_empty_value_set(self):
        self.bad({"allowed_values": {"a": []}})

    def test_bad_regex(self):
        self.bad({"patterns": {"a": "("}})

    def test_pattern_not_string(self):
        self.bad({"patterns": {"a": 3}})

    def test_bad_op(self):
        self.bad({"rules": [{"left": "a", "op": "~", "right": "b"}]})

    def test_rule_needs_one_rhs(self):
        self.bad({"rules": [{"left": "a", "op": ">"}]})
        self.bad({"rules": [{"left": "a", "op": ">", "right": "b", "right_value": 1}]})

    def test_right_value_numeric(self):
        self.bad({"rules": [{"left": "a", "op": ">", "right_value": "x"}]})
        self.bad({"rules": [{"left": "a", "op": ">", "right_value": True}]})

    def test_example_loads(self):
        with open(EXP, encoding="utf-8") as f:
            self.assertEqual(len(expectations.load(json.load(f))["rules"]), 1)


class RuleTests(unittest.TestCase):
    def test_allowed_values(self):
        fs = run(H, [row(i) for i in range(60)] + [row(100, region="mars-1")], {"allowed_values": {"region": ["ap-mumbai-1"]}})
        self.assertIn(("DQ011", "high"), rules(fs))

    def test_allowed_values_clean(self):
        fs = run(H, [row(i) for i in range(60)], {"allowed_values": {"region": ["ap-mumbai-1"]}})
        self.assertEqual(rules(fs), [])

    def test_numeric_allowed_values_compare_as_text(self):
        fs = run(H, [row(i, amount="10") for i in range(60)], {"allowed_values": {"amount": [10]}})
        self.assertEqual(rules(fs), [])

    def test_blank_not_a_violation(self):
        fs = run(H, [row(i, coupon="") for i in range(60)], {"patterns": {"coupon": "SAVE[0-9]+"}})
        self.assertEqual(rules(fs), [])

    def test_pattern_full_match(self):
        fs = run(H, [row(i) for i in range(59)] + [row(99, coupon="SAVE10X")], {"patterns": {"coupon": "SAVE[0-9]+"}})
        self.assertEqual(rules(fs), [("DQ012", "medium")])

    def test_missing_column(self):
        fs = run(H, [row(i) for i in range(60)], {"patterns": {"nope": "x"}, "allowed_values": {"nope": ["x"]}})
        self.assertEqual(sorted(rules(fs)), [("DQ011", "medium"), ("DQ012", "medium")])

    def test_cross_column_numbers(self):
        doc = {"rules": [{"left": "latency_ms", "op": "<=", "right": "amount"}]}
        self.assertEqual(rules(run(H, [row(i) for i in range(60)], doc)), [])
        fs = run(H, [row(i) for i in range(59)] + [row(99, lat="50")], doc)
        self.assertEqual(rules(fs), [("DQ013", "high")])
        self.assertEqual([f for f in fs if f.rule == "DQ013"][0].evidence["violations"], 1)

    def test_equal_boundary(self):
        doc = {"rules": [{"left": "latency_ms", "op": "<", "right": "amount"}]}
        fs = run(H, [row(i, amount="5", lat="5") for i in range(60)], doc)
        self.assertEqual(rules(fs), [("DQ013", "high")])

    def test_right_value(self):
        doc = {"rules": [{"left": "amount", "op": ">", "right_value": 0}]}
        fs = run(H, [row(i) for i in range(59)] + [row(99, amount="-1")], doc)
        self.assertEqual(rules(fs), [("DQ013", "high")])

    def test_dates_compare(self):
        h = ["order_id", "start", "end"]
        rows = [[str(i), "2026-09-01", "2026-09-02"] for i in range(3)] + [["9", "2026-09-05", "2026-09-02"]]
        base = profile.profile_file(write_csv(h, rows), "order_id")
        exp = expectations.load({"rules": [{"left": "end", "op": ">=", "right": "start"}]})
        fs = checks.check(base, write_csv(h, rows), expect=exp)
        self.assertEqual(rules(fs), [("DQ013", "high")])

    def test_mixed_kinds_reported_not_failed(self):
        h = ["order_id", "start", "end"]
        rows = [["1", "2026-09-01", "abc"], ["2", "2026-09-01", "2026-09-02"]]
        base = profile.profile_file(write_csv(h, rows), "order_id")
        exp = expectations.load({"rules": [{"left": "end", "op": ">=", "right": "start"}]})
        fs = checks.check(base, write_csv(h, rows), expect=exp)
        self.assertEqual(rules(fs), [("DQ013", "low")])

    def test_naive_vs_aware_dates_skipped(self):
        h = ["order_id", "start", "end"]
        rows = [["1", "2026-09-01T00:00:00", "2026-09-02T00:00:00Z"]]
        base = profile.profile_file(write_csv(h, rows), "order_id")
        exp = expectations.load({"rules": [{"left": "end", "op": ">=", "right": "start"}]})
        self.assertEqual(rules(checks.check(base, write_csv(h, rows), expect=exp)), [("DQ013", "low")])


class CompatTests(unittest.TestCase):
    def test_unchanged_without_expectations(self):
        base = profile.profile_file(BASE, "order_id")
        p = os.path.join(EX, "drifted.csv")
        a = [f.to_dict() for f in checks.check(base, p)]
        b = [f.to_dict() for f in checks.check(base, p, expect=None)]
        self.assertEqual(a, b)
        self.assertFalse([f for f in a if f["rule"] in ("DQ011", "DQ012", "DQ013")])

    def test_clean_batch_stays_clean_with_example_file(self):
        base = profile.profile_file(BASE, "order_id")
        with open(EXP, encoding="utf-8") as f:
            exp = expectations.load(json.load(f))
        self.assertEqual(checks.check(base, os.path.join(EX, "good.csv"), expect=exp), [])

    def test_drifted_gets_new_region_finding(self):
        base = profile.profile_file(BASE, "order_id")
        with open(EXP, encoding="utf-8") as f:
            exp = expectations.load(json.load(f))
        fs = checks.check(base, os.path.join(EX, "drifted.csv"), expect=exp)
        self.assertIn(("DQ011", "high"), rules(fs))


class CliTests(unittest.TestCase):
    def test_cli_flag(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            prof = os.path.join(t, "p.json")
            main(["profile", BASE, "--key", "order_id", "--output", prof])
            self.assertEqual(main(["check", prof, os.path.join(EX, "drifted.csv"), "--expectations", EXP, "--fail-on", "high"]), 2)

    def test_cli_bad_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            prof = os.path.join(t, "p.json")
            bad = os.path.join(t, "e.json")
            main(["profile", BASE, "--output", prof])
            with open(bad, "w") as f:
                f.write('{"patterns": {"a": "("}}')
            self.assertEqual(main(["check", prof, BASE, "--expectations", bad]), 1)


if __name__ == "__main__":
    unittest.main()
