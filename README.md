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
| `.github/rulesets/protect-main.json` | Branch ruleset for `main`: PR required, both gate checks required |
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

### Branch protection (ruleset)

A failing job only blocks a merge once `main` requires it.
`.github/rulesets/protect-main.json` defines that protection as a repository
ruleset for the default branch:

| Rule | Effect |
|---|---|
| Require a pull request | No direct pushes to `main`; every change goes through the gate. 0 approvals required, so a solo maintainer can still merge |
| Required status checks | **Quality gate (tests + contract)** and **Gate self-test (defective data must fail)** must pass, on a branch that is up to date with `main` |
| Block force pushes | History on `main` cannot be rewritten |
| Block deletion | `main` cannot be deleted |

No one can bypass it, repository admins included. The rollback runbook's
revert path already goes through a PR.

GitHub does not apply ruleset files from the repository automatically, so a
repository admin installs it once:

- **Web UI:** Settings → Rules → Rulesets → New ruleset → **Import a
  ruleset** → choose `.github/rulesets/protect-main.json` → Create.
- **CLI:** `gh api repos/jeffmoe/CST_660_Week5/rulesets --method POST --input .github/rulesets/protect-main.json`

To change the protection, edit the file in a PR, then re-import it (or `PUT`
it to `repos/{owner}/{repo}/rulesets/{id}`). A check named in the ruleset must
match the workflow job `name:` exactly. Rename a job and the ruleset waits for a
check that never reports, which blocks every merge.

The `staging` environment is created on the first promotion. Add required
reviewers there to make promotion a manual approval.

## Rollback runbook

Two recovery paths: **restore** the previously published dataset (fast; stops
consumer impact) and **revert** the bad change on `main` (slower; removes the
cause). They are complementary, not alternatives: a restore without a revert is
undone by the next merge, because `promote` republishes whatever `main` builds.

**Published version.** Each successful merge to `main` publishes a GitHub
pre-release tagged `v<contract version>-build.<run number>` with the bundle
`<tag>.tar.gz` (data, test and contract reports, contract, validator,
`RELEASE.json`, `SHA256SUMS`). The current staging dataset is the **newest
non-draft `v*-build.*` release**; consumers and the staging directory take that
one. Tags are never deleted or reused, so every published version stays
recoverable.

### Which path to use

| Situation | Path | Then |
|---|---|---|
| A published dataset is wrong and consumers may already be reading it (e.g. a number in the deposits packet does not reconcile) | **A. Restore** now | **B. Revert** to remove the cause |
| A bad change is on `main` but `promote` failed or did not run, so nothing bad was published | **B. Revert** only | — |
| A bad change is on `main` and was published, but no consumer reads staging until the fix lands | **B. Revert** only; the revert's own promotion replaces the dataset | — |
| The bad change edited `contracts/accounts.yaml` | **A. Restore** if the data is affected, then **C. Contract roll-forward** (a plain revert fails the gate) | — |
| Unsure | **A. Restore** — it is fast, needs no CI, and is itself reversible | Decide on B or C once consumers are protected |

Find the bad and last-good versions first:

```
gh release list --limit 10                     # newest first; note the bad tag and the one before it
gh release view <tag> --json body,targetCommitish
git log --oneline origin/main -10              # commit(s) that came in with the bad release
```

`RELEASE.json` in each bundle records the commit it was built from and the gate
run that approved it.

### A. Restore the previous published dataset

Expected time to recovery: **about 5–10 minutes**, all hands-on. No CI run is
involved.

1. **Withdraw the bad release.** Turning it into a draft hides it from
   consumers while keeping the tag and bundle for the incident record:
   ```
   gh release edit <bad-tag> --draft=true
   ```
   The newest non-draft release is now the last good one. (Web UI: Releases →
   the bad release → Edit → *Save as draft*.)
2. **Restore the staging directory** from the last good bundle and verify it
   byte for byte:
   ```
   gh release download <good-tag> --pattern '*.tar.gz' --dir restore
   tar -xzf restore/<good-tag>.tar.gz -C restore
   (cd restore/<good-tag> && sha256sum -c SHA256SUMS)    # every line must say OK
   ```
   Copy `restore/<good-tag>/` into the staging location. The `staging-<tag>`
   workflow artifact holds the same files but expires after 90 days; the
   release asset does not.
3. **Confirm** the restored data still meets the contract:
   ```
   python validate_contract.py restore/<good-tag>/accounts.yaml restore/<good-tag>/data --now <its balance date + 1 day>T06:00
   ```
4. **Notify** the consumers listed in `contracts/accounts.yaml` (Finance
   Analytics, Branch Operations) and the owner channel: which version was
   withdrawn, which is live, and when the bad one was first published, so
   reports built in between can be rerun.

Undo a restore by editing the draft back to published
(`gh release edit <bad-tag> --draft=false`).

### B. Revert the bad change on `main`

Expected time to recovery: **about 15–30 minutes**. The workflow itself takes
about a minute per run (the first merge run took 49 s end to end: gate 23 s,
self-test 23 s in parallel, promote 14 s), and a revert needs two runs, one on
the PR and one on the merge. Most of the time is review and approval, plus
staging-environment approval if required reviewers are configured.

1. Branch from `main` and revert. Merges into this repo have been fast-forward,
   so revert the commits themselves, newest first:
   ```
   git fetch origin
   git checkout -b revert/<short-name> origin/main
   git revert --no-edit <oldest-bad-sha>^..<newest-bad-sha>   # fast-forward or squash merge
   git revert --no-edit -m 1 <merge-sha>                      # if it came in as a merge commit
   git push -u origin revert/<short-name>
   ```
2. Open a PR to `main`. Both required checks must pass; a revert gets no
   exemption from the gate.
3. Merge. `promote` publishes a **new** tag (`build.<next run number>`) built
   from the reverted code. That release becomes the newest non-draft release and
   supersedes any restore from path A, so check that it validates before
   standing down.
4. Leave the bad tag in place (as a draft if path A ran). Do not delete tags:
   `promote` refuses to overwrite one, and the history is the audit trail.

### C. Contract changes: roll forward instead of reverting

`test_contract_change_follows_policy` compares the PR's contract with the one on
`main`. Reverting a contract edit takes the version backwards (e.g. v2.0.0 to
v1.0.0), which fails the version-bump check, so a plain `git revert` of a
contract change is blocked by the gate. Instead:

1. Restore the previous clauses by hand, **bump the version above the bad one**,
   and add a `change_log` entry for it.
2. If restoring the old clauses is non-breaking under the policy (for example,
   making a column required again, or narrowing a range), use a minor or patch
   bump. It passes the gate and follows the timeline in path B.
3. If it is breaking (for example, removing a column or an allowed value that
   the bad version added), the policy requires a new major version and 30 days'
   notice. The policy has no emergency exemption. Use path A to protect
   consumers in the meantime, and agree the change with the consumers named in
   the contract before shipping it.

## Walkthrough: the gate blocking a contract-breaking PR

[PR #1](https://github.com/jeffmoe/CST_660_Week5/pull/1) shows the gate
blocking a change for real, and then letting the corrected change through.

### The breaking change

Commit `0df4917` taught the producer (`scripts/generate_synthetic_data.py`) to
report long-standing open accounts with no customer activity for 12+ months as
`status = DORMANT`. The contract allows only `OPEN` and `CLOSED`. 46 of the 2,000
accounts in the clean dataset became `DORMANT`; nothing else in the data
changed. (The dormancy flag is drawn from a child random generator so the rest
of the dataset stays identical and only the status change is under test.)

### What CI reported

[Run 37951625862](https://github.com/jeffmoe/CST_660_Week5/actions/runs/37951625862):

| Job / step | Result |
|---|---|
| Quality gate → Generate synthetic data | passed |
| Quality gate → **Run pytest suite** | **failed** (exit code 1) |
| Quality gate → Run contract validator | skipped: an earlier step failed, so the job stopped |
| Quality gate → Upload build artifacts | passed (`if: always()`, so the reports are available) |
| Gate self-test | passed: defective data still fails as it should |
| Promote | skipped: only runs on a merge to `main` |

Two of the 43 tests failed. Each is read below.

**1. `test_schema_and_data_types[accounts]`**

```
E   AssertionError: accounts: 92 schema/data-type violation(s)
FAIL  accounts schema: 92 violating row(s)
          column                            check bad_value   account_id ... close_date   status
csv_line
2          (row)  close_date_set_only_when_closed      None  LCB00000001 ...        NaN  DORMANT
2         status         isin(['OPEN', 'CLOSED'])   DORMANT  LCB00000001 ...        NaN  DORMANT
```

- **What it says:** two pandera rules fail on each of the 46 `DORMANT` rows (92
  violations). `isin(['OPEN', 'CLOSED'])` is the domain check: `DORMANT` is not
  a known status. `close_date_set_only_when_closed` is the cross-column rule
  "`close_date` is null exactly when the account is `OPEN`": a `DORMANT`
  account has no close date but is not `OPEN`, so the table no longer has a
  consistent definition of an open account.
- **Code or data defect:** a **code defect** in this PR. The producer code
  started emitting a value its published contract does not allow; the source
  accounts did not change. (The same failure with no code change in the PR
  would mean an **upstream data defect**: core banking began sending `DORMANT`
  on its own. The check output looks the same; the PR diff tells you which.)
- **Minimal correct fix:** stop emitting `DORMANT`. Not: add `DORMANT` to
  `STATUSES` in `tests/schemas.py`. That would silence the test without
  telling any consumer.

**2. `test_payload_satisfies_contract`**

```
E   AssertionError: validate_contract.py exited 1: payload violates accounts.yaml
[FAIL] accounts.status: in ['OPEN', 'CLOSED'] -- 46 row(s) hold a value outside the allowed set
42 clauses: 40 passed, 1 failed, 1 skipped
CONTRACT VIOLATED: 1 clause(s) failed. Producer contact: Core Banking Data Platform <core-banking-data@lumenbank.example>, #core-banking-data
```

- **What it says:** the producer-consumer contract itself is broken: 46
  delivered rows carry a status outside the agreed set. The report names the
  owner to contact. (The contract has no close-date rule, so this check flags
  each row once, against the `status` clause.)
- **Code or data defect:** the same **code defect**, seen from the consumer's
  side.
- **Minimal correct fix:** the same one. Do not loosen the contract by adding
  `DORMANT` to `allowed_values`:
  - **It would mislead consumers.** The CFO packet and branch reconciliations
    treat `status = OPEN` as "live deposit account". Dormant accounts would
    silently drop out of open-account counts and average balances. That is
    the same silent misstatement as the fan-out incident, in the other
    direction.
  - **The gate would block it anyway.** Adding an allowed value is listed as
    breaking in `change_policy`, so `test_contract_change_follows_policy`
    fails unless the contract goes to v2.0.0 with 30 days' notice to Finance
    Analytics and Branch Operations.

### The fix

The fix commit restores the producer to emitting only `OPEN` and `CLOSED`.
Legally a dormant account is still open, so `OPEN` is the correct value under
the current contract. The PR then contains only this README section, and both
checks pass.

If the business does need dormancy, ship it as a versioned contract change
rather than by repurposing `status`:
- **Preferred:** add a nullable `is_dormant` boolean column. `add_column` is
  non-breaking under the policy, so it is a v1.1.0 with a change-log entry, and
  no consumer's `status` logic changes.
- **Only if consumers agree:** add `DORMANT` to `status` as v2.0.0, announced at
  least 30 days ahead, after Finance Analytics confirms how dormant balances
  should be counted.

### How to read any failing gate

1. Open the failed step's log. The `short test summary info` block at the end
   lists the failing tests. Each test's *Captured stdout call* section prints
   the violating rows by `csv_line`.
2. **Check the PR diff.** If it touches the producer (`scripts/`), the
   contract, or the checks, the failure is a code defect in the PR. If it
   doesn't, and the failure appears on new data, it is an upstream data defect:
   do not merge "fixes" to the checks; contact the owner named in the report.
3. **Fix the cause, not the check.** Change a check or the contract only when
   the rule itself was wrong. Any contract change goes through the change
   policy: version bump, change-log entry, and notice for breaking changes.
