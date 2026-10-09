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
| `contracts/accounts.yaml` | Producer-consumer data contract for the accounts table and its balance feed |
| `validate_contract.py` | Validates a payload (and optionally a contract change) against a contract |
| `requirements.txt` | Pinned dependencies |
| `.github/workflows/quality-gate.yml` | CI gate on pull requests to `main`; staging promotion and release tag on merge |
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
account IDs, orphan branch codes, null customer IDs, a duplicated `BR017`
row in `branch_reference` that fans out joins, and one daily balance posted
×100. Use `--clean` for a defect-free
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
| `test_business_rules.py` | No account's ledger balance moves more than 300% day over day. Accounts opened or closed inside the window are excluded (lifecycle funding and run-off legitimately exceed 300%), and only days with a prior-day balance of at least $1,000 are evaluated, since percent change against a near-zero balance is meaningless |
| `test_contract.py` | Runs the [data contract](#data-contract): the contract file is well formed, the payload satisfies every clause, and any edit to the contract follows the breaking-change policy relative to the version on `origin/main` |

Each failure is reported by one test only: schemas leave columns nullable so
nulls fail only the not-null tests, and foreign-key tests skip null keys.

Every test prints the rows that violated it, indexed by `csv_line` (the line in
the source CSV), and pytest shows that output under *Captured stdout call*. A
join failure also prints the `branch_reference` rows that caused it, e.g.:

```
accounts rows before join: 2003; after left join: 2060
FAIL  accounts left-joined to branch_reference keeps one row per account: 57 violating row(s)
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

Against the committed defective dataset, 9 of 43 tests fail and 1 is skipped:

| Failing test | Injected defect |
|---|---|
| `test_primary_key_is_unique[branch_reference]` | Duplicate `BR017` key |
| `test_join_to_branch_reference_preserves_row_count[left]` | `BR017` fan-out (2003 → 2060 rows) |
| `test_join_to_branch_reference_preserves_row_count[inner]` | `BR017` fan-out plus 5 orphan accounts dropped (2003 → 2055 rows) |
| `test_primary_key_is_unique[accounts]` | 3 duplicated account IDs |
| `test_foreign_key_resolves[accounts.branch_code->branch_reference.branch_code]` | 5 orphan branch codes |
| `test_schema_and_data_types[accounts]` | Malformed branch code `BR7` |
| `test_required_column_not_null[accounts.customer_id]` | 4 null customer IDs |
| `test_daily_balance_change_within_300_percent` | Decimal-shift spike: one balance posted ×100 for a day (+9,900%) |
| `test_payload_satisfies_contract` | Null customer IDs, `BR7`, duplicate account IDs, and the spike breaking the $750,000 balance ceiling (5 contract clauses) |

`test_contract_change_follows_policy` is skipped until the contract exists on
`origin/main`; from then on it checks every contract edit against that version.
Against a `--clean` dataset all 42 runnable tests pass. The largest day-over-day move there
is about 250%, so the 300% threshold has headroom.

## Data contract

`contracts/accounts.yaml` is the machine-readable agreement between the Core
Banking Data Platform team (producer) and Finance Analytics and Branch
Operations (consumers). It declares:

| Clause | Content |
|---|---|
| Owner | Producing team, email, Slack channel, escalation address |
| Consumers | Who depends on the data and for what |
| Columns | Name, type (`string`, `date`, `decimal`, `integer`, `boolean`), nullability, and patterns for IDs and branch codes. `accounts` and its `daily_balances` feed each have a primary key and reject undeclared columns |
| Allowed values | `status` in `OPEN`/`CLOSED`, `product_type` enum, `interest_rate` 0–10%, `ledger_balance` and `available_balance` between −$5,000 and $750,000 |
| Freshness SLA | Newest `balance_date` no more than 30 hours past the end of that business day (America/Chicago), i.e. yesterday's balances by 06:00 |
| Breaking-change policy | Semver; breaking changes (removing a column, changing a type, making a column nullable, adding or removing an enum value, widening a range, relaxing the SLA, ...) need a major version bump and a change-log notice at least 30 days before they take effect |

The accounts table itself has no balance column, so the balance range applies
to the `daily_balances` feed delivered with it, which is also what the
freshness SLA measures.

```
python validate_contract.py contracts/accounts.yaml data/synthetic --now 2026-10-01T06:00
python validate_contract.py NEW_CONTRACT.yaml PAYLOAD_DIR --baseline contracts/accounts.yaml
```

The validator checks that the contract is well formed (pydantic), then checks
every clause against the payload and prints one `[PASS]`/`[FAIL]`/`[SKIP]` line
per clause, with the violating rows (by CSV line) under each failure. With
`--baseline`, it diffs the two contract versions, classifies each change using
the baseline's policy (anything not explicitly non-breaking counts as breaking),
and enforces the version bump and notification window. `--now` pins the
validation time; without it the freshness SLA is measured against the current
time, so the static synthetic data will always be stale.

### In the pytest run

`tests/test_contract.py` runs the validator inside the test suite, so the
contract is enforced by the same gate as the other checks. Two settings in
`pytest.ini` control it, and each can be overridden on the command line:

| Setting | Default | Purpose |
|---|---|---|
| `validation_time` / `--validation-time=` | `2026-10-01T06:00` | Time freshness is measured at. Pinned because the synthetic data is static; set it empty for a live feed |
| `contract_baseline_ref` / `--contract-baseline-ref=` | `origin/main` | Git ref holding the contract in force. If the working-tree contract differs from it, the change must follow the breaking-change policy |

For example, bumping the contract to v1.1.0 and adding a `DORMANT` status with
three days' notice fails `test_contract_change_follows_policy`: adding an enum
value is breaking, so it needs v2.0.0 and 30 days' notice.

### Exit codes

Exit codes: `0` all clauses pass, `1` any violation, `2` the contract is invalid
or the payload directory is missing.

Against the committed dataset at `--now 2026-10-01T06:00`, 5 of 42 clauses fail:
4 null `customer_id` values, the malformed `BR7` branch code, 3 duplicated
account IDs, and the ×100 balance spike, which breaks the $750,000 ceiling on
both `ledger_balance` and `available_balance`. A `--clean` dataset satisfies the
contract.

## CI/CD: quality gate and promotion

`.github/workflows/quality-gate.yml` runs on every pull request to `main` and
on every push (merge) to `main`.

| Job | When | What it does |
|---|---|---|
| `quality-gate` | PR and merge | Installs `requirements.txt`, generates a **clean** synthetic dataset into `build/data`, runs the pytest suite against it (JUnit report in `build/reports`), then runs `validate_contract.py`, passing the contract on the base branch as `--baseline` so contract edits must follow the breaking-change policy. Any failing step fails the job. Data and reports are uploaded as the `quality-gate-<sha>` artifact, even on failure |
| `gate-self-test` | PR and merge | Generates the **defective** dataset and requires both pytest and the validator to exit `1`. If a change weakens a control so the injected defects get through, this job fails |
| `promote` | Merge to `main` only, after both jobs pass | Assembles `staging/<tag>/` with the gate's data and reports, the contract, the validator, `requirements.txt`, `RELEASE.json`, and `SHA256SUMS`. Publishes it as the `staging-<tag>` artifact in the `staging` environment, pushes an annotated tag `v<contract version>-build.<run number>` (e.g. `v1.0.0-build.42`), and creates a GitHub pre-release with the bundle attached |

The gate runs on a clean dataset because the committed one is defective by
design and would block every PR. The self-test runs on the defective dataset to
prove the gate still catches the failure classes it exists for.

`VALIDATION_TIME` (workflow `env`) pins the freshness check to
`2026-10-01T06:00`, the same as `pytest.ini`.

### Repository settings needed

A failing job only blocks a merge once branch protection requires it. In
**Settings → Branches → main** (or a ruleset), require the status checks
**Quality gate (tests + contract)** and **Gate self-test (defective data must
fail)**. The `staging` environment is created on the first promotion; add
required reviewers there to make promotion a manual approval.
