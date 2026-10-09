"""Schema and data-type checks: columns, types, and value domains per table."""

from __future__ import annotations

import pandas as pd
import pytest

from tests.reporting import print_violations
from tests.schemas import SCHEMAS, SchemaErrors


@pytest.mark.parametrize("table", SCHEMAS)
def test_schema_and_data_types(tables, table):
    df = tables[table]
    try:
        SCHEMAS[table].validate(df, lazy=True)
        failures = pd.DataFrame()
    except SchemaErrors as err:
        failures = summarise(err.failure_cases, df)

    print_violations(f"{table} schema", failures)
    violations = len(failures)
    assert violations == 0, f"{table}: {violations} schema/data-type violation(s)"


def summarise(failure_cases: pd.DataFrame, df: pd.DataFrame) -> pd.DataFrame:
    """One line per (row, column, bad value), with the full source row attached."""
    fc = failure_cases.rename(columns={"index": "csv_line", "failure_case": "bad_value"})
    # A value that fails coercion is reported again by the follow-on dtype check.
    fc = fc[~fc["check"].astype(str).str.startswith("dtype(")].copy()
    # Table-level failures (missing or unexpected columns, or a cross-column check
    # that could not run) have no csv_line and print with an empty row.
    fc["csv_line"] = pd.to_numeric(fc["csv_line"], errors="coerce").astype("Int64")
    # Cross-column checks are reported once per column; collapse to one line per row.
    row_level = (fc["schema_context"] == "DataFrameSchema") & fc["csv_line"].notna()
    fc.loc[row_level, ["column", "bad_value"]] = ["(row)", None]
    fc = fc.drop_duplicates(["csv_line", "column", "check", "bad_value"])
    fc = fc[["csv_line", "column", "check", "bad_value"]]
    rows = df.reset_index().astype({"csv_line": "Int64"})
    merged = fc.merge(rows, on="csv_line", how="left").sort_values(["csv_line", "column"], na_position="first")
    return merged.set_index("csv_line")
