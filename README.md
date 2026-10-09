# CST_660_Week5
Quality Gates, Tests, Contracts, and a CI/CD Promotion Path

Automated data-quality controls for Lumen Community Bank's deposits reporting.
Last quarter a duplicate key in the branch reference table fanned out a join and
overstated average customer balances in the CFO's weekly deposits packet for
eleven days. The test suite here blocks that failure class, and its neighbours,
before a change reaches production.

## Layout

| Path | What it is |
|---|---|
| `scripts/generate_synthetic_data.py` | Seeded generator for the synthetic core-banking dataset |
| `data/synthetic/` | Generated CSVs, `manifest.json` of injected defects, and a [data README](data/synthetic/README.md) |
| `tests/` | pytest + pandera data-quality suite |
| `requirements.txt` | Pinned dependencies |
| `pytest.ini` | pytest configuration |

## Setup

```
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

## Synthetic data

`python scripts/generate_synthetic_data.py` writes `branch_reference`, `customers`,
`accounts` and 90 days of `daily_balances` (about 2,000 accounts across 42
branches) to `data/synthetic/`. The dataset deliberately contains duplicate
account IDs, orphan branch codes, null customer IDs, and a duplicated `BR017`
row in `branch_reference` that fans out joins. Use `--clean` for a defect-free
copy and `--out <dir>` to write elsewhere.

## Data-quality tests

```
pytest                                      # test data/synthetic
pytest --data-dir=path/to/other/csvs        # test another dataset
```

`LUMEN_DATA_DIR` works in place of `--data-dir`. Write the option with `=`:
given as two separate arguments, pytest mistakes the directory for a test path.

| Module | Checks |
|---|---|
| `test_schema.py` | Pandera schema per table: exact column set and order, every value parses as its declared type (dates, decimals), ID formats, allowed values for region/segment/product/status, interest rate range, and cross-column rules (close date after open date and only on closed accounts, available ≤ ledger, customer since after date of birth) |
| `test_primary_keys.py` | Key uniqueness: `branch_code`, `customer_id`, `account_id`, and `(account_id, balance_date)` |
| `test_not_null.py` | Not-null on every required column, one test per column |
| `test_referential_integrity.py` | `accounts.branch_code → branch_reference`, plus `accounts.customer_id → customers`, `customers.home_branch_code → branch_reference`, `daily_balances.account_id → accounts` |
| `test_join_cardinality.py` | Row-count assertion: joining `accounts` to `branch_reference` (left and inner) must return exactly one row per account |

Each failure is reported by one test only: schemas leave columns nullable so
nulls fail only the not-null tests, and foreign-key tests skip null keys.

Every test prints the rows that violated it, indexed by `csv_line` (the line in
the source CSV), and pytest shows that output under *Captured stdout call*. A
join failure also prints the `branch_reference` rows that caused it, e.g.:

```
accounts rows before join: 2003; after left join: 2058
FAIL  accounts left-joined to branch_reference keeps one row per account: 55 violating row(s)
           account_id customer_id branch_code  product_type   open_date ... rows_after_join
csv_line
6         LCB00000005    C0000371       BR017  MONEY_MARKET  2023-12-14 ...               2
...
FAIL  branch_reference rows behind the change: 2 violating row(s)
         branch_code                branch_name         city state   region ...
csv_line
18             BR017          Lumen Minneapolis  Minneapolis    MN    North ...
19             BR017  Lumen Minneapolis (Metro)  Minneapolis    MN  Central ...
```

### Expected results

Against the committed defective dataset, 7 of 39 tests fail, one for each kind of
injected defect:

| Failing test | Injected defect |
|---|---|
| `test_primary_key_is_unique[branch_reference]` | Duplicate `BR017` key |
| `test_join_to_branch_reference_preserves_row_count[left]` | `BR017` fan-out (2003 → 2058 rows) |
| `test_join_to_branch_reference_preserves_row_count[inner]` | `BR017` fan-out plus 5 orphan accounts dropped (2003 → 2053 rows) |
| `test_primary_key_is_unique[accounts]` | 3 duplicated account IDs |
| `test_foreign_key_resolves[accounts.branch_code->branch_reference.branch_code]` | 5 orphan branch codes |
| `test_schema_and_data_types[accounts]` | Malformed branch code `BR7` |
| `test_required_column_not_null[accounts.customer_id]` | 4 null customer IDs |

Against a `--clean` dataset all 39 pass.
