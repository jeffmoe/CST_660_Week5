"""Business-rule checks on balance behaviour.

Day-over-day balance swing: flag any account whose ledger balance changes by
more than 300% from one day to the next. A move that large on an established
account almost always means a posting error (a misplaced decimal, a batch
loaded twice, a sign flip) rather than real customer activity, and it distorts
average-balance reporting just as badly as a join fan-out does.
"""

from __future__ import annotations

import pandas as pd

from tests.reporting import print_violations

MAX_DAILY_CHANGE = 3.00  # 300%, as a fraction of the prior day's balance

# Percent change is only meaningful against a material starting balance. A
# checking account at $4.33 that receives a $2,000 paycheck has moved 47,700%,
# and one at $0 has moved an infinite amount, yet both are routine. Below this
# prior-day balance the rule is not evaluated.
MIN_PRIOR_BALANCE = 1_000.00


def test_daily_balance_change_within_300_percent(tables):
    balances = tables["daily_balances"].reset_index()
    accounts = tables["accounts"]

    balances["balance_date"] = pd.to_datetime(balances["balance_date"], errors="coerce")
    balances["ledger_balance"] = pd.to_numeric(balances["ledger_balance"], errors="coerce")
    window_start, window_end = balances["balance_date"].min(), balances["balance_date"].max()

    # Exclude accounts opened or closed inside the reporting window. Their
    # balance histories are dominated by lifecycle events, not ordinary
    # activity, and those events legitimately exceed 300%:
    #   * Opening: a new account is usually opened with a token deposit and
    #     funded in full a day or two later (e.g. $50 then a $25,000 CD
    #     rollover), and its first day in the window has no prior-day balance
    #     to compare against at all.
    #   * Closing: the account is drained in the run-up to closure, so its last
    #     days sit near zero where any residual credit (final interest, a fee
    #     reversal) is a huge percentage of almost nothing.
    # Flagging these would bury real posting errors under expected noise. The
    # lifecycle events themselves are covered by schema rules on open_date and
    # close_date instead.
    open_date = pd.to_datetime(accounts["open_date"], errors="coerce")
    close_date = pd.to_datetime(accounts["close_date"], errors="coerce")
    in_window = open_date.between(window_start, window_end) | close_date.between(window_start, window_end)
    # An account_id duplicated in accounts is excluded if any of its rows qualifies.
    lifecycle_accounts = set(accounts.loc[in_window, "account_id"])
    print(f"window {window_start.date()} to {window_end.date()}; "
          f"excluding {len(lifecycle_accounts)} account(s) opened or closed in the window")

    steady = balances[~balances["account_id"].isin(lifecycle_accounts)]
    steady = steady.sort_values(["account_id", "balance_date"])
    previous = steady.groupby("account_id")[["balance_date", "ledger_balance"]].shift()
    steady = steady.assign(prior_date=previous["balance_date"], prior_ledger_balance=previous["ledger_balance"])

    # Compare consecutive calendar days only; a gap is a completeness problem, not a swing.
    consecutive = steady["balance_date"] - steady["prior_date"] == pd.Timedelta(days=1)
    material = steady["prior_ledger_balance"].abs() >= MIN_PRIOR_BALANCE
    evaluated = steady[consecutive & material].copy()
    evaluated["pct_change"] = (
        (evaluated["ledger_balance"] - evaluated["prior_ledger_balance"]) / evaluated["prior_ledger_balance"].abs()
    )

    swings = evaluated[evaluated["pct_change"].abs() > MAX_DAILY_CHANGE]
    report = swings.set_index("csv_line")[
        ["account_id", "prior_date", "prior_ledger_balance", "balance_date", "ledger_balance", "pct_change"]
    ].assign(pct_change=lambda d: (d["pct_change"] * 100).round(1).astype(str) + "%")

    print_violations(f"daily ledger balance change <= {MAX_DAILY_CHANGE:.0%} day over day", report)
    swing_count = len(swings)
    assert swing_count == 0, (
        f"{swing_count} day-over-day balance change(s) above {MAX_DAILY_CHANGE:.0%} "
        f"on {swings['account_id'].nunique()} account(s)"
    )
