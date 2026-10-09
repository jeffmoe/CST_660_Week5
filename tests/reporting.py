"""Print violating rows so a failed check says exactly what to fix.

pytest shows a test's captured stdout when it fails, so the rows printed here
appear under "Captured stdout call" in the failure report.
"""

from __future__ import annotations

import pandas as pd

MAX_PRINTED_ROWS = 50


def print_violations(check: str, violations: pd.DataFrame) -> None:
    if violations.empty:
        print(f"PASS  {check}: no violating rows")
        return
    print(f"FAIL  {check}: {len(violations)} violating row(s)")
    with pd.option_context("display.max_columns", None, "display.width", 250):
        print(violations.head(MAX_PRINTED_ROWS).to_string())
    if len(violations) > MAX_PRINTED_ROWS:
        print(f"... and {len(violations) - MAX_PRINTED_ROWS} more")
