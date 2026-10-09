"""Join row-count guard: accounts -> branch_reference must stay one row per account.

This is the control for last quarter's deposits incident. A duplicate
branch_code in branch_reference made the join fan out, silently multiplying
those branches' balances. A left join catches fan-out; an inner join also
catches accounts dropped because their branch_code has no reference row.
"""

from __future__ import annotations

import pytest

from tests.reporting import print_violations


@pytest.mark.parametrize("how", ["left", "inner"])
def test_join_to_branch_reference_preserves_row_count(accounts, branch_reference, how):
    joined = accounts.reset_index().merge(
        branch_reference, on="branch_code", how=how, suffixes=("", "_branch")
    )

    # Rows per source account row after the join: anything other than 1 is a violation.
    rows_after = joined.groupby("csv_line").size().reindex(accounts.index, fill_value=0)
    changed = accounts.assign(rows_after_join=rows_after)[rows_after != 1]
    rows_before, rows_joined = len(accounts), len(joined)
    print(f"accounts rows before join: {rows_before}; after {how} join: {rows_joined}")
    print_violations(f"accounts {how}-joined to branch_reference keeps one row per account", changed)

    if not changed.empty:
        keys = branch_reference[branch_reference["branch_code"].isin(changed["branch_code"])]
        print_violations("branch_reference rows behind the change", keys.sort_values("branch_code"))

    assert rows_joined == rows_before, (
        f"{how} join changed accounts row count from {rows_before} to {rows_joined}"
    )
