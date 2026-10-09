"""Pandera schemas: the column, data-type, and value-domain contract per table.

Columns are declared nullable here on purpose. Required-column checks live in
test_not_null.py so each failure class is reported by exactly one test, and a
null never shows up twice as both a schema and a not-null failure.
"""

from __future__ import annotations

import pandera.pandas as pa
from pandera.pandas import Check, Column, DataFrameSchema

BRANCH_CODE = Check.str_matches(r"^BR\d{3}$", name="branch_code_format")
CUSTOMER_ID = Check.str_matches(r"^C\d{7}$", name="customer_id_format")
ACCOUNT_ID = Check.str_matches(r"^LCB\d{8}$", name="account_id_format")

REGIONS = ["Central", "East", "North", "South", "West"]
SEGMENTS = ["RETAIL", "SMALL_BUSINESS", "PRIVATE"]
PRODUCTS = ["CHECKING", "SAVINGS", "MONEY_MARKET", "CD"]
STATUSES = ["OPEN", "CLOSED"]


def col(dtype, *checks: Check) -> Column:
    # Every source column arrives as text; coerce=True makes the schema prove
    # each value parses as the declared type and report the ones that do not.
    return Column(dtype, checks=list(checks), coerce=dtype is not str, nullable=True)


DATE = "datetime64[ns]"

SCHEMAS: dict[str, DataFrameSchema] = {
    "branch_reference": DataFrameSchema(
        {
            "branch_code": col(str, BRANCH_CODE),
            "branch_name": col(str),
            "city": col(str),
            "state": col(str, Check.str_matches(r"^[A-Z]{2}$", name="state_format")),
            "region": col(str, Check.isin(REGIONS)),
            "opened_date": col(DATE),
            "is_active": col(str, Check.isin(["True", "False"])),
        },
        strict=True,
        ordered=True,
    ),
    "customers": DataFrameSchema(
        {
            "customer_id": col(str, CUSTOMER_ID),
            "first_name": col(str),
            "last_name": col(str),
            "date_of_birth": col(DATE),
            "customer_since": col(DATE),
            "segment": col(str, Check.isin(SEGMENTS)),
            "home_branch_code": col(str, BRANCH_CODE),
        },
        checks=[
            Check(lambda df: df["customer_since"] > df["date_of_birth"],
                  name="customer_since_after_date_of_birth", ignore_na=True),
        ],
        strict=True,
        ordered=True,
    ),
    "accounts": DataFrameSchema(
        {
            "account_id": col(str, ACCOUNT_ID),
            "customer_id": col(str, CUSTOMER_ID),
            "branch_code": col(str, BRANCH_CODE),
            "product_type": col(str, Check.isin(PRODUCTS)),
            "open_date": col(DATE),
            "close_date": col(DATE),
            "status": col(str, Check.isin(STATUSES)),
            "interest_rate": col(float, Check.in_range(0, 0.10)),
        },
        checks=[
            Check(lambda df: df["close_date"].isna() | (df["close_date"] >= df["open_date"]),
                  name="close_date_on_or_after_open_date"),
            Check(lambda df: df["close_date"].isna() == (df["status"] == "OPEN"),
                  name="close_date_set_only_when_closed"),
        ],
        strict=True,
        ordered=True,
    ),
    "daily_balances": DataFrameSchema(
        {
            "account_id": col(str, ACCOUNT_ID),
            "balance_date": col(DATE),
            "ledger_balance": col(float),
            "available_balance": col(float),
        },
        checks=[
            Check(lambda df: df["available_balance"] <= df["ledger_balance"],
                  name="available_not_above_ledger", ignore_na=True),
        ],
        strict=True,
        ordered=True,
    ),
}

SchemaErrors = pa.errors.SchemaErrors
