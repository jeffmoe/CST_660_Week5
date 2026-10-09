"""Not-null checks on every column a downstream model depends on."""

from __future__ import annotations

import pytest

from tests.reporting import print_violations

REQUIRED_COLUMNS = {
    "branch_reference": ["branch_code", "branch_name", "city", "state", "region", "opened_date", "is_active"],
    "customers": ["customer_id", "first_name", "last_name", "date_of_birth", "customer_since", "segment",
                  "home_branch_code"],
    # close_date is legitimately null for open accounts.
    "accounts": ["account_id", "customer_id", "branch_code", "product_type", "open_date", "status",
                 "interest_rate"],
    "daily_balances": ["account_id", "balance_date", "ledger_balance", "available_balance"],
}

CASES = [(table, column) for table, columns in REQUIRED_COLUMNS.items() for column in columns]


@pytest.mark.parametrize(("table", "column"), CASES, ids=[f"{t}.{c}" for t, c in CASES])
def test_required_column_not_null(tables, table, column):
    df = tables[table]
    nulls = df[df[column].isna()]

    print_violations(f"{table}.{column} not null", nulls)
    null_count = len(nulls)
    assert null_count == 0, f"{table}.{column}: {null_count} null value(s)"
