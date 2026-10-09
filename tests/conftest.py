"""Shared fixtures: load the core-banking CSVs once per test session.

Every table is read with all columns as strings so the schema tests, not
pandas' type inference, decide whether a value is a valid date or number.
The index is the 1-based line number in the CSV (header is line 1), so any
violating row printed by a test can be found directly in the source file.

Point the suite at another dataset with ``pytest --data-dir=<dir>`` or the
``LUMEN_DATA_DIR`` environment variable. Data-contract settings
(``validation_time``, ``contract_baseline_ref``) live in pytest.ini.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = REPO_ROOT / "data" / "synthetic"
TABLES = ("branch_reference", "customers", "accounts", "daily_balances")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--data-dir",
        default=os.environ.get("LUMEN_DATA_DIR", str(DEFAULT_DATA_DIR)),
        help="directory containing the core-banking CSVs (default: data/synthetic)",
    )
    parser.addini("validation_time", "ISO time freshness is measured at; empty means now", default="")
    parser.addini("contract_baseline_ref", "git ref holding the contract in force", default="origin/main")
    parser.addoption("--validation-time", help="override the validation_time ini setting")
    parser.addoption("--contract-baseline-ref", help="override the contract_baseline_ref ini setting")


def load_table(data_dir: Path, name: str) -> pd.DataFrame:
    df = pd.read_csv(data_dir / f"{name}.csv", dtype=str, keep_default_na=False, na_values=[""])
    df.index = pd.RangeIndex(2, len(df) + 2, name="csv_line")
    return df


@pytest.fixture(scope="session")
def data_dir(request: pytest.FixtureRequest) -> Path:
    path = Path(request.config.getoption("--data-dir"))
    missing = [t for t in TABLES if not (path / f"{t}.csv").exists()]
    if missing:
        pytest.exit(f"{path} is missing {', '.join(missing)}.csv", returncode=2)
    return path


@pytest.fixture(scope="session")
def tables(data_dir: Path) -> dict[str, pd.DataFrame]:
    return {name: load_table(data_dir, name) for name in TABLES}


@pytest.fixture(scope="session")
def accounts(tables) -> pd.DataFrame:
    return tables["accounts"]


@pytest.fixture(scope="session")
def branch_reference(tables) -> pd.DataFrame:
    return tables["branch_reference"]
