"""Generate a synthetic Lumen Community Bank core-banking dataset.

Writes four CSVs (branch_reference, customers, accounts, daily_balances) to
data/synthetic/ plus a manifest describing every deliberately injected defect.
Output is fully deterministic for a given --seed, so downstream tests can assert
against exact violation counts.

Injected defects (the failure classes the quality gates must catch):
  * duplicate account_id rows in accounts
  * accounts whose branch_code has no match in branch_reference (orphans)
  * accounts with a null customer_id
  * one branch_reference row that duplicates an existing branch_code, so any
    accounts -> branch_reference join fans out for that branch

Standard library only; run with:  python scripts/generate_synthetic_data.py
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "data" / "synthetic"

AS_OF = date(2026, 9, 30)
N_DAYS = 90
WINDOW_START = AS_OF - timedelta(days=N_DAYS - 1)

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

FIRST_NAMES = [
    "James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael", "Linda",
    "David", "Elizabeth", "William", "Barbara", "Richard", "Susan", "Joseph",
    "Jessica", "Thomas", "Sarah", "Charles", "Karen", "Daniel", "Lisa", "Matthew",
    "Nancy", "Anthony", "Betty", "Mark", "Sandra", "Steven", "Ashley", "Paul",
    "Emily", "Andrew", "Donna", "Joshua", "Michelle", "Kevin", "Carol", "Brian",
    "Amanda", "Erik", "Ingrid", "Lars", "Greta", "Nils", "Astrid", "Olaf", "Hilda",
]
LAST_NAMES = [
    "Johnson", "Anderson", "Nelson", "Peterson", "Olson", "Larson", "Hanson",
    "Miller", "Schmidt", "Mueller", "Schultz", "Wagner", "Becker", "Hoffman",
    "Smith", "Brown", "Davis", "Wilson", "Moore", "Taylor", "Thomas", "Jackson",
    "White", "Harris", "Martin", "Thompson", "Garcia", "Martinez", "Robinson",
    "Clark", "Lewis", "Walker", "Hall", "Young", "King", "Wright", "Lopez",
    "Hill", "Scott", "Green", "Adams", "Baker", "Gonzalez", "Carlson", "Lindgren",
]

# product -> (weight, lognormal mu, lognormal sigma, rate range)
PRODUCTS = {
    "CHECKING":     (0.48, math.log(3_200), 1.0, (0.0000, 0.0010)),
    "SAVINGS":      (0.30, math.log(9_500), 1.1, (0.0040, 0.0125)),
    "MONEY_MARKET": (0.12, math.log(38_000), 0.9, (0.0200, 0.0375)),
    "CD":           (0.10, math.log(25_000), 0.8, (0.0350, 0.0475)),
}


def branch_code(i: int) -> str:
    return f"BR{i:03d}"


def build_branches(rng: random.Random) -> list[dict]:
    rows = []
    for i, (city, state, region) in enumerate(BRANCHES, start=1):
        opened = date(1968, 1, 1) + timedelta(days=rng.randint(0, 365 * 55))
        rows.append({
            "branch_code": branch_code(i),
            "branch_name": f"Lumen {city}" + (f" {state}" if city == "Springfield" else ""),
            "city": city,
            "state": state,
            "region": region,
            "opened_date": opened.isoformat(),
            "is_active": "true",
        })
    return rows


def build_customers(rng: random.Random, n_branches: int) -> list[dict]:
    rows = []
    for i in range(1, N_CUSTOMERS + 1):
        dob = date(1940, 1, 1) + timedelta(days=rng.randint(0, 365 * 64))
        since = date(1995, 1, 1) + timedelta(days=rng.randint(0, (AS_OF - date(1995, 1, 1)).days - 30))
        rows.append({
            "customer_id": f"C{i:07d}",
            "first_name": rng.choice(FIRST_NAMES),
            "last_name": rng.choice(LAST_NAMES),
            "date_of_birth": dob.isoformat(),
            "customer_since": since.isoformat(),
            "segment": rng.choices(["RETAIL", "SMALL_BUSINESS", "PRIVATE"], [0.86, 0.11, 0.03])[0],
            "home_branch_code": branch_code(rng.randint(1, n_branches)),
        })
    return rows


def build_accounts(rng: random.Random, customers: list[dict]) -> list[dict]:
    # Every customer gets at least one account; the remainder go to a random
    # subset so a realistic share of customers hold several products.
    owners = [c for c in customers]
    owners += rng.choices(customers, k=N_ACCOUNTS - len(customers))
    rng.shuffle(owners)

    product_names = list(PRODUCTS)
    weights = [PRODUCTS[p][0] for p in product_names]
    rows = []
    for i, cust in enumerate(owners, start=1):
        product = rng.choices(product_names, weights)[0]
        lo, hi = PRODUCTS[product][3]
        cust_since = date.fromisoformat(cust["customer_since"])
        # ~4% of accounts open inside the reporting window.
        if rng.random() < 0.04:
            opened = WINDOW_START + timedelta(days=rng.randint(1, N_DAYS - 5))
        else:
            span = max((WINDOW_START - cust_since).days, 1)
            opened = cust_since + timedelta(days=rng.randint(0, span - 1))
        closed = ""
        status = "OPEN"
        # ~2% close during the window.
        if rng.random() < 0.02:
            close_day = max(opened, WINDOW_START) + timedelta(days=rng.randint(5, 60))
            if close_day <= AS_OF:
                closed = close_day.isoformat()
                status = "CLOSED"
        # Accounts are usually held at the customer's home branch.
        home = cust["home_branch_code"]
        acct_branch = home if rng.random() < 0.85 else branch_code(rng.randint(1, len(BRANCHES)))
        rows.append({
            "account_id": f"LCB{i:08d}",
            "customer_id": cust["customer_id"],
            "branch_code": acct_branch,
            "product_type": product,
            "open_date": opened.isoformat(),
            "close_date": closed,
            "status": status,
            "interest_rate": f"{rng.uniform(lo, hi):.4f}",
        })
    return rows


def build_daily_balances(rng: random.Random, accounts: list[dict]) -> list[dict]:
    rows = []
    for acct in accounts:
        product = acct["product_type"]
        _, mu, sigma, _ = PRODUCTS[product]
        bal = round(rng.lognormvariate(mu, sigma), 2)
        rate = float(acct["interest_rate"])
        payroll = rng.lognormvariate(math.log(1_800), 0.5)
        opened = date.fromisoformat(acct["open_date"])
        closed = date.fromisoformat(acct["close_date"]) if acct["close_date"] else None
        start = max(opened, WINDOW_START)
        end = closed - timedelta(days=1) if closed else AS_OF
        if opened >= WINDOW_START:
            bal = round(bal * rng.uniform(0.1, 0.6), 2)  # new accounts start smaller

        d = start
        while d <= end:
            if product == "CHECKING":
                if d.day in (1, 15):
                    bal += payroll
                if rng.random() < 0.55:
                    # Spending roughly offsets two paydays a month, so balances do not drift.
                    bal -= rng.lognormvariate(math.log(payroll * 0.095), 0.7)
                if bal < 0 and rng.random() < 0.9:
                    bal += payroll * 0.5  # overdraft transfer from savings
            elif product in ("SAVINGS", "MONEY_MARKET"):
                bal *= 1 + rate / 365
                if rng.random() < 0.03:
                    bal += rng.choice([-1, 1]) * rng.lognormvariate(math.log(600), 0.8)
                bal = max(bal, 0.0)
            else:  # CD: flat principal with interest capitalised monthly
                if d.day == 1:
                    bal *= 1 + rate / 12
            bal = round(bal, 2)
            # Available balance trails ledger by pending holds on transaction accounts.
            hold = round(rng.uniform(0, 250), 2) if product == "CHECKING" and rng.random() < 0.2 else 0.0
            rows.append({
                "account_id": acct["account_id"],
                "balance_date": d.isoformat(),
                "ledger_balance": f"{bal:.2f}",
                "available_balance": f"{bal - hold:.2f}",
            })
            d += timedelta(days=1)
    return rows


def inject_defects(rng: random.Random, branches: list[dict], accounts: list[dict]) -> dict:
    """Mutate the clean tables in place and return a manifest of what was broken."""
    manifest: dict = {}

    # 1. Fan-out key: a branch re-org inserted a new row for BR017 without
    #    retiring the old one, so branch_code is no longer unique.
    fanout_code = "BR017"
    original = next(b for b in branches if b["branch_code"] == fanout_code)
    dup = dict(original, region="Central", branch_name=original["branch_name"] + " (Metro)")
    branches.insert(branches.index(original) + 1, dup)
    manifest["branch_reference_duplicate_key"] = {
        "branch_code": fanout_code,
        "row_count_for_key": 2,
        "description": "Second branch_reference row for BR017 (region re-org) "
                       "without retiring the original; joins on branch_code double these accounts.",
    }

    # Pick distinct victims for each remaining defect so counts stay exact.
    pool = rng.sample(range(len(accounts)), 12)

    # 2. Orphan branch codes: codes that never existed or belong to a closed branch
    #    that was purged from the reference table.
    orphan_codes = ["BR043", "BR099", "BR000", "BR043", "BR7"]
    orphans = []
    for idx, code in zip(pool[0:5], orphan_codes):
        accounts[idx]["branch_code"] = code
        orphans.append({"account_id": accounts[idx]["account_id"], "branch_code": code})
    manifest["orphan_branch_codes"] = {"count": len(orphans), "rows": orphans}

    # 3. Null customer IDs.
    nulls = []
    for idx in pool[5:9]:
        accounts[idx]["customer_id"] = ""
        nulls.append(accounts[idx]["account_id"])
    manifest["null_customer_ids"] = {"count": len(nulls), "account_ids": nulls}

    # 4. Duplicate account IDs: one exact double-load and two conflicting rows
    #    (same ID, different attributes). Appended near the source row.
    dups = []
    exact_src = accounts[pool[9]]
    accounts.insert(pool[9] + 1, dict(exact_src))
    dups.append({"account_id": exact_src["account_id"], "kind": "exact_duplicate_row"})
    for idx in pool[10:12]:
        src = next(a for a in accounts if a["account_id"] == f"LCB{idx + 1:08d}")
        conflict = dict(src)
        conflict["branch_code"] = branch_code(rng.randint(1, len(BRANCHES)))
        conflict["product_type"] = rng.choice([p for p in PRODUCTS if p != src["product_type"]])
        accounts.insert(accounts.index(src) + 1, conflict)
        dups.append({"account_id": src["account_id"], "kind": "conflicting_attributes"})
    # Counted after every mutation so it reflects the final accounts table.
    manifest["branch_reference_duplicate_key"]["account_rows_affected_by_fanout"] = sum(
        1 for a in accounts if a["branch_code"] == fanout_code)

    manifest["duplicate_account_ids"] = {
        "distinct_ids_duplicated": len(dups),
        "extra_rows": len(dups),
        "rows": dups,
    }
    return manifest


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seed", type=int, default=660)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--clean", action="store_true", help="skip defect injection")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    branches = build_branches(rng)
    customers = build_customers(rng, len(branches))
    accounts = build_accounts(rng, customers)
    # Balances are generated from the clean account list: one series per real account.
    balances = build_daily_balances(rng, accounts)

    manifest = {} if args.clean else inject_defects(rng, branches, accounts)

    args.out.mkdir(parents=True, exist_ok=True)
    write_csv(args.out / "branch_reference.csv", branches)
    write_csv(args.out / "customers.csv", customers)
    write_csv(args.out / "accounts.csv", accounts)
    write_csv(args.out / "daily_balances.csv", balances)

    full_manifest = {
        "seed": args.seed,
        "as_of_date": AS_OF.isoformat(),
        "window_start": WINDOW_START.isoformat(),
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
