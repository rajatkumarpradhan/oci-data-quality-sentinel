# oci-data-quality-sentinel

Offline data-quality and drift checks for tabular CSV data, aimed at the kind of batch exports that feed OCI Data Science or Autonomous Database loads. Build a baseline profile from a known-good CSV, then check later batches against it.

**Scope, stated plainly.** Portfolio project. It reads local CSV files only. The bundled data is synthetic (an orders-style table). It has not been run on real production data, calls no OCI service, and spends nothing. Findings are review priorities from sample statistics, not proof of a data problem.

## Usage

```
python -m pip install -e .
dq-sentinel profile examples/baseline.csv --key order_id --output profile.json
dq-sentinel check profile.json examples/good.csv                       # 0 findings
dq-sentinel check profile.json examples/drifted.csv --fail-on high     # exit code 2
dq-sentinel check profile.json new.csv --timestamp-col created_at --max-age-days 2
python -m unittest discover -s tests -v
```

`profile` options: `--key` (column that must be unique) and `--ignore-drift COL ...` (ids, sequence numbers). `check` options: `--output report.json`, `--fail-on {high,medium,low}`, `--now` (freshness reference time).

## Rules

| Rule | Severity | What it flags |
|---|---|---|
| DQ001 | high | Baseline column missing |
| DQ002 | medium | New column not in baseline |
| DQ003 | high | Column type changed (numeric / categorical) |
| DQ004 | medium / high | Null rate up by 5 points / 20 points |
| DQ005 | medium / high | Numeric distribution shift: PSI >= 0.10 (medium), PSI >= 0.25 or KS >= 0.30 (high) |
| DQ006 | medium | 3% or more of values outside the baseline min/max |
| DQ007 | low / medium / high | Category mix shift (PSI) or new category values |
| DQ008 | high | Duplicate values in the key column |
| DQ009 | low | Batch under 50 rows: drift statistics skipped |
| DQ010 | medium | Newest timestamp older than `--max-age-days` |

PSI uses the baseline's own decile bins and measured bin shares, so tied baselines are handled. KS compares against a stored baseline sample (up to 2000 points). The PSI cut-offs are common rules of thumb, not guarantees.

## Limits

- Only null, type, distribution, range, key and freshness checks. No cross-column rules, referential checks or custom expectations.
- High-cardinality text columns (more than 20 distinct values, such as timestamps and free text) are only checked for nulls, not drift.
- Type inference is "all non-null values parse as numbers or not"; dates are treated as text.
- With small baselines, the min/max range check is noisy at the tails.
- A distribution shift can be legitimate (a promotion, a new region). The tool says what changed, not why.
- CSV only; no Parquet, database or Object Storage readers.

## Tests

26 unit tests: statistics (PSI, KS, quantiles, ties), profiling errors, every rule, a false-positive check over 20 same-distribution batches (expects zero findings), and the CLI on the bundled clean and drifted examples. CI runs on Python 3.10, 3.11 and 3.12.

## License

MIT
