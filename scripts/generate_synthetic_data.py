"""Generate a synthetic Lumen Community Bank core-banking dataset.

Writes four CSVs (branch_reference, customers, accounts, daily_balances) to
data/synthetic/ plus a manifest describing every deliberately injected defect.
Output is fully deterministic for a given --seed (with the pinned versions in
requirements.txt), so downstream tests can assert against exact violation counts.

Injected defects (the failure classes the quality gates must catch):
  * duplicate account_id rows in accounts
  * accounts whose branch_code has no match in branch_reference (orphans)
  * accounts with a null customer_id
  * one branch_reference row that duplicates an existing branch_code, so any
    accounts -> branch_reference join fans out for that branch
  * one daily balance with a decimal-shift error (x100 for a single day)

Run with:  python scripts/generate_synthetic_data.py
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from faker import Faker

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "data" / "synthetic"

AS_OF = pd.Timestamp("2026-09-30")
N_DAYS = 90
WINDOW_START = AS_OF - pd.Timedelta(days=N_DAYS - 1)
DATES = pd.date_range(WINDOW_START, AS_OF, freq="D")

N_ACCOUNTS = 2000
N_CUSTOMERS = 1450

# (city, state, region) -- 42 branches across the Midwest footprint.
BRANCHES = [
    ("Des Moines", "IA", "Central"), ("Ames", "IA", "Central"),
    ("Ankeny", "IA", "Central"), ("West Des Moines", "IA", "Central"),
    ("Cedar Rapids", "IA", "East"), ("Iowa City", "IA", "East"),
    ("Davenport", "IA", "East"), ("Dubuque", "IA", "East"),
    ("Waterloo", "IA", "North"), ("Mason City", "IA", "North"),
    ("Sioux City", "IA", "West"), ("Council Bluffs", "IA", "West"),
    ("Omaha", "NE", "West"), ("Lincoln", "NE", "West"),
    ("Grand Island", "NE", "West"), ("Kearney", "NE", "West"),
    ("Minneapolis", "MN", "North"), ("St. Paul", "MN", "North"),
    ("Rochester", "MN", "North"), ("Mankato", "MN", "North"),
    ("St. Cloud", "MN", "North"), ("Madison", "WI", "East"),
    ("La Crosse", "WI", "East"), ("Eau Claire", "WI", "North"),
    ("Green Bay", "WI", "East"), ("Rockford", "IL", "East"),
    ("Peoria", "IL", "East"), ("Moline", "IL", "East"),
    ("Springfield", "IL", "South"), ("Champaign", "IL", "South"),
    ("Bloomington", "IL", "South"), ("Kansas City", "MO", "South"),
    ("Columbia", "MO", "South"), ("St. Joseph", "MO", "South"),
    ("Springfield", "MO", "South"), ("Topeka", "KS", "South"),
    ("Lawrence", "KS", "South"), ("Manhattan", "KS", "South"),
    ("Sioux Falls", "SD", "West"), ("Fargo", "ND", "North"),
    ("Bismarck", "ND", "North"), ("Grand Forks", "ND", "North"),
]
assert len(BRANCHES) == 42

# product -> (mix weight, opening-balance median, lognormal sigma, (rate lo, rate hi))
PRODUCTS = {
    "CHECKING":     (0.48, 3_200, 1.0, (0.0000, 0.0010)),
    "SAVINGS":      (0.30, 9_500, 1.1, (0.0040, 0.0125)),
    "MONEY_MARKET": (0.12, 38_000, 0.9, (0.0200, 0.0375)),
    "CD":           (0.10, 25_000, 0.8, (0.0350, 0.0475)),
}


def branch_codes(idx) -> np.ndarray:
    """1-based branch numbers -> 'BR001' style codes."""
    return np.char.add("BR", np.char.zfill(np.asarray(idx).astype(str), 3))


def random_dates(rng: np.random.Generator, start, end, size: int) -> pd.DatetimeIndex:
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    offsets = rng.integers(0, (end - start).days + 1, size=size)
    return start + pd.to_timedelta(offsets, unit="D")


def build_branches(rng: np.random.Generator) -> pd.DataFrame:
    df = pd.DataFrame(BRANCHES, columns=["city", "state", "region"])
    df.insert(0, "branch_code", branch_codes(np.arange(1, len(df) + 1)))
    # Two Springfields: disambiguate by state.
    suffix = np.where(df["city"] == "Springfield", " " + df["state"], "")
    df.insert(1, "branch_name", "Lumen " + df["city"] + suffix)
    df["opened_date"] = random_dates(rng, "1968-01-01", "2022-12-31", len(df))
    df["is_active"] = True
    return df


def build_customers(rng: np.random.Generator, fake: Faker, n_branches: int) -> pd.DataFrame:
    dob = random_dates(rng, "1940-01-01", "2004-12-31", N_CUSTOMERS)
    # Relationships start no earlier than 1995 and no earlier than the customer's 18th birthday.
    earliest = pd.Series(dob + pd.DateOffset(years=18)).clip(lower=pd.Timestamp("1995-01-01"))
    latest = AS_OF - pd.Timedelta(days=30)
    since = earliest + pd.to_timedelta(rng.integers(0, (latest - earliest).dt.days.to_numpy() + 1), unit="D")
    return pd.DataFrame({
        "customer_id": [f"C{i:07d}" for i in range(1, N_CUSTOMERS + 1)],
        "first_name": [fake.first_name() for _ in range(N_CUSTOMERS)],
        "last_name": [fake.last_name() for _ in range(N_CUSTOMERS)],
        "date_of_birth": dob,
        "customer_since": since,
        "segment": rng.choice(["RETAIL", "SMALL_BUSINESS", "PRIVATE"], size=N_CUSTOMERS, p=[0.86, 0.11, 0.03]),
        "home_branch_code": branch_codes(rng.integers(1, n_branches + 1, size=N_CUSTOMERS)),
    })


def build_accounts(rng: np.random.Generator, customers: pd.DataFrame) -> pd.DataFrame:
    # Every customer gets at least one account; the remainder go to a random
    # subset so a realistic share of customers hold several products.
    owner_idx = np.concatenate([
        np.arange(len(customers)),
        rng.integers(0, len(customers), size=N_ACCOUNTS - len(customers)),
    ])
    rng.shuffle(owner_idx)
    owners = customers.iloc[owner_idx].reset_index(drop=True)
    n = len(owners)

    names = list(PRODUCTS)
    product = rng.choice(names, size=n, p=[PRODUCTS[p][0] for p in names])
    lo = np.array([PRODUCTS[p][3][0] for p in product])
    hi = np.array([PRODUCTS[p][3][1] for p in product])

    # Most accounts open between the customer's start date and the window;
    # ~4% open inside the reporting window.
    since = owners["customer_since"]
    span_days = np.maximum((WINDOW_START - since).dt.days.to_numpy(), 1)
    opened = since + pd.to_timedelta(rng.integers(0, span_days), unit="D")
    new_in_window = rng.random(n) < 0.04
    opened = opened.where(~new_in_window, WINDOW_START + pd.to_timedelta(rng.integers(1, N_DAYS - 4, size=n), unit="D"))

    # ~2% close during the window.
    close_candidate = opened.clip(lower=WINDOW_START) + pd.to_timedelta(rng.integers(5, 61, size=n), unit="D")
    closes = (rng.random(n) < 0.02) & (close_candidate <= AS_OF)
    close_date = close_candidate.where(closes)

    # ~4% of long-standing open accounts have had no customer-initiated activity
    # for 12+ months; core banking now flags these as dormant. Drawn from a
    # child generator so the rest of the dataset is unchanged.
    dormancy_rng = rng.spawn(1)[0]
    dormant = ~closes & (opened < WINDOW_START - pd.DateOffset(years=3)) & (dormancy_rng.random(n) < 0.04)

    # Accounts are usually held at the customer's home branch.
    other_branch = branch_codes(rng.integers(1, len(BRANCHES) + 1, size=n))
    branch = np.where(rng.random(n) < 0.85, owners["home_branch_code"], other_branch)

    return pd.DataFrame({
        "account_id": [f"LCB{i:08d}" for i in range(1, n + 1)],
        "customer_id": owners["customer_id"],
        "branch_code": branch,
        "product_type": product,
        "open_date": opened,
        "close_date": close_date,
        "status": np.select([closes, dormant], ["CLOSED", "DORMANT"], default="OPEN"),
        "interest_rate": np.round(rng.uniform(lo, hi), 4),
    })


def build_daily_balances(rng: np.random.Generator, accounts: pd.DataFrame) -> pd.DataFrame:
    """Simulate an (accounts x days) balance matrix per product, then flatten."""
    n, t = len(accounts), len(DATES)
    product = accounts["product_type"].to_numpy()
    rate = accounts["interest_rate"].to_numpy()[:, None]

    median = np.array([PRODUCTS[p][1] for p in product], dtype=float)
    sigma = np.array([PRODUCTS[p][2] for p in product])
    start = rng.lognormal(np.log(median), sigma)
    is_new = (accounts["open_date"] >= WINDOW_START).to_numpy()
    start = np.where(is_new, start * rng.uniform(0.1, 0.6, size=n), start)  # new accounts start smaller

    day = np.arange(t)[None, :]
    dom = DATES.day.to_numpy()

    # Checking: payroll on the 1st and 15th; spending roughly offsets two paydays
    # a month so balances do not drift. Each customer keeps a cushion of 40-100%
    # of a paycheck: a sweep from savings tops the account back up whenever it
    # dips below, so balances do not sit at $0 waiting for payday.
    payroll = rng.lognormal(np.log(1_800), 0.5, size=(n, 1))
    cushion = payroll * rng.uniform(0.4, 1.0, size=(n, 1))
    spend = (rng.random((n, t)) < 0.55) * rng.lognormal(np.log(payroll * 0.08), 0.7, size=(n, t))
    checking = start[:, None] + np.cumsum(np.isin(dom, (1, 15)) * payroll - spend, axis=1)
    checking += np.maximum.accumulate(np.clip(cushion - checking, 0, None), axis=1)

    # Savings / money market: daily accrual plus occasional deposits or withdrawals.
    flows = (rng.random((n, t)) < 0.03) * rng.choice([-1, 1], size=(n, t)) * rng.lognormal(np.log(600), 0.8, size=(n, t))
    savings = np.clip(start[:, None] * (1 + rate / 365) ** (day + 1) + np.cumsum(flows, axis=1), 0, None)

    # CD: flat principal, interest capitalised on the 1st of each month.
    months_elapsed = np.cumsum(dom == 1)[None, :]
    cd = start[:, None] * (1 + rate / 12) ** months_elapsed

    ledger = np.select(
        [product[:, None] == "CHECKING", product[:, None] == "CD"],
        [checking, cd],
        default=savings,
    ).round(2)

    # Available balance trails ledger by pending holds on transaction accounts.
    holds = ((product[:, None] == "CHECKING") & (rng.random((n, t)) < 0.2)) * rng.uniform(0, 250, size=(n, t))
    available = (ledger - holds.round(2)).round(2)

    # Only emit rows for days the account was open.
    first = accounts["open_date"].clip(lower=WINDOW_START).to_numpy()[:, None]
    last = (accounts["close_date"] - pd.Timedelta(days=1)).fillna(AS_OF).to_numpy()[:, None]
    dates = DATES.to_numpy()[None, :]
    open_mask = (dates >= first) & (dates <= last)

    rows, cols = np.nonzero(open_mask)
    return pd.DataFrame({
        "account_id": accounts["account_id"].to_numpy()[rows],
        "balance_date": DATES[cols],
        "ledger_balance": ledger[rows, cols],
        "available_balance": available[rows, cols],
    })


def inject_defects(rng: np.random.Generator, branches: pd.DataFrame, accounts: pd.DataFrame,
                   balances: pd.DataFrame):
    """Return broken copies of branches/accounts/balances and a manifest of what was broken."""
    manifest: dict = {}
    accounts = accounts.copy()
    balances = balances.copy()

    # 1. Fan-out key: a branch re-org inserted a new row for BR017 without
    #    retiring the old one, so branch_code is no longer unique.
    fanout_code = "BR017"
    pos = branches.index[branches["branch_code"] == fanout_code][0]
    dup = branches.loc[[pos]].assign(region="Central", branch_name=lambda d: d["branch_name"] + " (Metro)")
    branches = pd.concat([branches.loc[:pos], dup, branches.loc[pos + 1:]], ignore_index=True)
    manifest["branch_reference_duplicate_key"] = {
        "branch_code": fanout_code,
        "row_count_for_key": 2,
        "description": "Second branch_reference row for BR017 (region re-org) "
                       "without retiring the original; joins on branch_code double these accounts.",
    }

    # Distinct victims for each remaining defect so counts stay exact.
    victims = rng.choice(len(accounts), size=12, replace=False)

    # 2. Orphan branch codes: codes that never existed, or a closed branch that
    #    was purged from the reference table.
    orphan_idx = victims[0:5]
    accounts.loc[orphan_idx, "branch_code"] = ["BR043", "BR099", "BR000", "BR043", "BR7"]
    manifest["orphan_branch_codes"] = {
        "count": len(orphan_idx),
        "rows": accounts.loc[orphan_idx, ["account_id", "branch_code"]].to_dict("records"),
    }

    # 3. Null customer IDs.
    null_idx = victims[5:9]
    accounts.loc[null_idx, "customer_id"] = None
    manifest["null_customer_ids"] = {
        "count": len(null_idx),
        "account_ids": accounts.loc[null_idx, "account_id"].tolist(),
    }

    # 4. Duplicate account IDs: one exact double-load and two conflicting rows
    #    (same ID, different branch and product), placed next to the source row.
    exact = accounts.loc[[victims[9]]]
    conflicts = accounts.loc[victims[10:12]].copy()
    conflicts["branch_code"] = branch_codes(rng.integers(1, len(BRANCHES) + 1, size=len(conflicts)))
    conflicts["product_type"] = [
        rng.choice([p for p in PRODUCTS if p != cur]) for cur in conflicts["product_type"]
    ]
    extras = pd.concat([exact, conflicts])
    accounts = (
        pd.concat([accounts, extras])
        .sort_index(kind="stable")  # duplicates land directly after their source row
        .reset_index(drop=True)
    )
    manifest["duplicate_account_ids"] = {
        "distinct_ids_duplicated": len(extras),
        "extra_rows": len(extras),
        "rows": [{"account_id": a, "kind": "exact_duplicate_row"} for a in exact["account_id"]]
              + [{"account_id": a, "kind": "conflicting_attributes"} for a in conflicts["account_id"]],
    }

    # Counted after every mutation so it reflects the final accounts table.
    manifest["branch_reference_duplicate_key"]["account_rows_affected_by_fanout"] = int(
        (accounts["branch_code"] == fanout_code).sum())

    # 5. Decimal-shift spike: one day's balance posted x100 (a misplaced decimal
    #    in a batch file). The victim is an otherwise clean, non-checking account
    #    open for the whole window with a material prior-day balance, so this is
    #    the only defect that touches it.
    clean = accounts[
        ~accounts["account_id"].duplicated(keep=False)
        & accounts["customer_id"].notna()
        & accounts["branch_code"].isin(branches["branch_code"])
        & (accounts["branch_code"] != fanout_code)
        & (accounts["open_date"] < WINDOW_START)
        & accounts["close_date"].isna()
        & (accounts["product_type"] != "CHECKING")
    ]
    spike_date = DATES[rng.integers(10, N_DAYS - 10)]
    prior = balances[(balances["balance_date"] == spike_date - pd.Timedelta(days=1))
                     & balances["account_id"].isin(clean["account_id"])
                     & (balances["ledger_balance"] >= 1_000)]
    spike_account = prior["account_id"].iloc[rng.integers(len(prior))]
    hit = (balances["account_id"] == spike_account) & (balances["balance_date"] == spike_date)
    before = float(balances.loc[hit, "ledger_balance"].iloc[0])
    balances.loc[hit, ["ledger_balance", "available_balance"]] *= 100
    manifest["balance_decimal_shift"] = {
        "count": 1,
        "account_id": spike_account,
        "balance_date": spike_date.date().isoformat(),
        "ledger_balance_correct": round(before, 2),
        "ledger_balance_posted": round(before * 100, 2),
    }

    return branches, accounts, balances, manifest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seed", type=int, default=660)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--clean", action="store_true", help="skip defect injection")
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    fake = Faker("en_US")
    fake.seed_instance(args.seed)

    branches = build_branches(rng)
    customers = build_customers(rng, fake, len(branches))
    accounts = build_accounts(rng, customers)
    # Balances are generated from the clean account list: one series per real account.
    balances = build_daily_balances(rng, accounts)

    manifest = {}
    if not args.clean:
        branches, accounts, balances, manifest = inject_defects(rng, branches, accounts, balances)

    args.out.mkdir(parents=True, exist_ok=True)
    csv_opts = {"index": False, "date_format": "%Y-%m-%d", "lineterminator": "\n"}
    branches.to_csv(args.out / "branch_reference.csv", **csv_opts)
    customers.to_csv(args.out / "customers.csv", **csv_opts)
    accounts.to_csv(args.out / "accounts.csv", float_format="%.4f", **csv_opts)
    balances.to_csv(args.out / "daily_balances.csv", float_format="%.2f", **csv_opts)

    full_manifest = {
        "seed": args.seed,
        "as_of_date": AS_OF.date().isoformat(),
        "window_start": WINDOW_START.date().isoformat(),
        "days": N_DAYS,
        "row_counts": {
            "branch_reference": len(branches),
            "customers": len(customers),
            "accounts": len(accounts),
            "daily_balances": len(balances),
        },
        "injected_defects": manifest,
    }
    (args.out / "manifest.json").write_text(json.dumps(full_manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(full_manifest["row_counts"], indent=2))


if __name__ == "__main__":
    main()
