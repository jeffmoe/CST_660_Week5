"""Referential integrity: every foreign key must resolve to a parent row.

Null foreign keys are skipped here; test_not_null.py reports those.
"""

from __future__ import annotations

import pytest

from tests.reporting import print_violations

# (child table, child column, parent table, parent column)
FOREIGN_KEYS = [
    ("accounts", "branch_code", "branch_reference", "branch_code"),
    ("accounts", "customer_id", "customers", "customer_id"),
    ("customers", "home_branch_code", "branch_reference", "branch_code"),
    ("daily_balances", "account_id", "accounts", "account_id"),
]


@pytest.mark.parametrize(
    ("child", "child_col", "parent", "parent_col"),
    FOREIGN_KEYS,
    ids=[f"{c}.{cc}->{p}.{pc}" for c, cc, p, pc in FOREIGN_KEYS],
)
def test_foreign_key_resolves(tables, child, child_col, parent, parent_col):
    child_df, parent_df = tables[child], tables[parent]
    orphans = child_df[child_df[child_col].notna() & ~child_df[child_col].isin(parent_df[parent_col])]

    print_violations(f"{child}.{child_col} -> {parent}.{parent_col}", orphans)
    orphan_count = len(orphans)
    assert orphan_count == 0, (
        f"{child}: {orphan_count} row(s) reference a {child_col} missing from {parent}.{parent_col}: "
        f"{sorted(orphans[child_col].unique())}"
    )
