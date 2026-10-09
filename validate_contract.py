"""Validate a data payload against a producer-consumer data contract.

    python validate_contract.py CONTRACT PAYLOAD_DIR [--baseline OLD_CONTRACT] [--now ISO_TIMESTAMP]

Checks, in order:
  1. The contract itself is well formed (pydantic model below).
  2. Every dataset clause against the payload: file present, declared and
     undeclared columns, data types, nullability, patterns, allowed values,
     value ranges, and primary-key uniqueness.
  3. The freshness SLA against the validation time (--now, default: current time).
  4. The breaking-change policy, when --baseline gives the previous contract
     version: classifies every change, then requires a major version bump and a
     change-log notice of at least the notification window for breaking ones.

Prints one line per clause, followed by the violating rows for any failure.
Exit status: 0 all clauses pass, 1 one or more violations, 2 the contract or
payload could not be loaded.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

EXIT_OK, EXIT_VIOLATIONS, EXIT_UNUSABLE = 0, 1, 2

SEMVER = r"^\d+\.\d+\.\d+$"
EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

ChangeKind = Literal[
    "add_dataset", "remove_dataset",
    "add_column", "remove_column",
    "change_column_type", "change_column_pattern",
    "make_column_nullable", "make_column_required",
    "change_primary_key",
    "add_allowed_value", "remove_allowed_value",
    "widen_range", "narrow_range",
    "relax_freshness_sla", "tighten_freshness_sla",
    "allow_undeclared_columns", "disallow_undeclared_columns",
]


# --------------------------------------------------------------------------- #
# Contract model: anything missing, misspelled, or inconsistent fails loading. #
# --------------------------------------------------------------------------- #

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Range(Strict):
    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def _ordered(self):
        if self.min is None and self.max is None:
            raise ValueError("range needs min, max, or both")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"range min {self.min} is above max {self.max}")
        return self


class Column(Strict):
    name: str
    type: Literal["string", "date", "decimal", "integer", "boolean"]
    nullable: bool
    pattern: str | None = None
    allowed_values: list[str] | None = None
    range: Range | None = None
    description: str | None = None

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, v):
        if v is not None:
            re.compile(v)
        return v

    @model_validator(mode="after")
    def _range_is_numeric(self):
        if self.range and self.type not in ("decimal", "integer"):
            raise ValueError(f"column {self.name}: range only applies to decimal or integer columns")
        return self


class Dataset(Strict):
    name: str
    file: str
    primary_key: list[str] = Field(min_length=1)
    allow_undeclared_columns: bool = False
    columns: list[Column] = Field(min_length=1)

    @model_validator(mode="after")
    def _keys_declared(self):
        names = [c.name for c in self.columns]
        if len(names) != len(set(names)):
            raise ValueError(f"dataset {self.name}: duplicate column names")
        by_name = {c.name: c for c in self.columns}
        for key in self.primary_key:
            if key not in by_name:
                raise ValueError(f"dataset {self.name}: primary key column {key} is not declared")
            if by_name[key].nullable:
                raise ValueError(f"dataset {self.name}: primary key column {key} cannot be nullable")
        return self

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)


class Owner(Strict):
    team: str
    role: Literal["producer"]
    email: str = Field(pattern=EMAIL)
    slack: str
    escalation: str = Field(pattern=EMAIL)


class Consumer(Strict):
    team: str
    use: str
    email: str = Field(pattern=EMAIL)


class Freshness(Strict):
    dataset: str
    column: str
    max_age_hours: float = Field(gt=0)
    timezone: str

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, v):
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as e:
            raise ValueError(f"unknown timezone {v}") from e
        return v


class Sla(Strict):
    freshness: Freshness


class ChangeLogEntry(Strict):
    version: str = Field(pattern=SEMVER)
    announced_on: date
    effective_on: date
    breaking: bool
    summary: str


class ChangePolicy(Strict):
    versioning: Literal["semver"]
    notification_window_days: int = Field(ge=0)
    notify: list[Literal["consumers"]]
    breaking_changes: list[ChangeKind]
    non_breaking_changes: list[ChangeKind]
    change_log: list[ChangeLogEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self):
        both = set(self.breaking_changes) & set(self.non_breaking_changes)
        if both:
            raise ValueError(f"change kinds listed as both breaking and non-breaking: {sorted(both)}")
        for entry in self.change_log:
            if entry.effective_on < entry.announced_on:
                raise ValueError(f"change_log {entry.version}: effective_on is before announced_on")
        return self

    def entry(self, version: str) -> ChangeLogEntry | None:
        return next((e for e in self.change_log if e.version == version), None)


class ContractMeta(Strict):
    id: str
    version: str = Field(pattern=SEMVER)
    status: Literal["draft", "active", "deprecated"]
    effective_on: date
    description: str


class Contract(Strict):
    contract: ContractMeta
    owner: Owner
    consumers: list[Consumer] = Field(min_length=1)
    datasets: list[Dataset] = Field(min_length=1)
    sla: Sla
    change_policy: ChangePolicy

    @model_validator(mode="after")
    def _references_resolve(self):
        f = self.sla.freshness
        ds = self.dataset(f.dataset)
        if ds is None:
            raise ValueError(f"freshness dataset {f.dataset} is not declared")
        col = ds.column(f.column)
        if col is None or col.type != "date":
            raise ValueError(f"freshness column {f.dataset}.{f.column} must be a declared date column")
        if self.change_policy.entry(self.contract.version) is None:
            raise ValueError(f"change_log has no entry for the current version {self.contract.version}")
        return self

    def dataset(self, name: str) -> Dataset | None:
        return next((d for d in self.datasets if d.name == name), None)


def load_contract(path: Path) -> Contract:
    with path.open(encoding="utf-8") as f:
        return Contract.model_validate(yaml.safe_load(f))


# --------------------------------------------------------------------------- #
# Reporting                                                                    #
# --------------------------------------------------------------------------- #

@dataclass
class Result:
    clause: str
    status: Literal["PASS", "FAIL", "SKIP"]
    detail: str = ""
    rows: pd.DataFrame | None = None


class Report:
    def __init__(self, max_rows: int):
        self.results: list[Result] = []
        self.max_rows = max_rows

    def check(self, clause: str, violations: pd.DataFrame, detail: str) -> None:
        """Record a row-level clause: it passes when there are no violating rows."""
        if violations.empty:
            self.results.append(Result(clause, "PASS"))
        else:
            self.results.append(Result(clause, "FAIL", f"{len(violations)} row(s) {detail}", violations))

    def add(self, clause: str, ok: bool, detail: str = "") -> None:
        self.results.append(Result(clause, "PASS" if ok else "FAIL", detail))

    def skip(self, clause: str, detail: str) -> None:
        self.results.append(Result(clause, "SKIP", detail))

    @property
    def failures(self) -> list[Result]:
        return [r for r in self.results if r.status == "FAIL"]

    def print(self) -> None:
        for r in self.results:
            line = f"[{r.status}] {r.clause}"
            print(f"{line} -- {r.detail}" if r.detail else line)
            if r.rows is not None:
                shown = r.rows.head(self.max_rows)
                with pd.option_context("display.max_columns", None, "display.width", 250):
                    print("\n".join("       " + l for l in shown.to_string().splitlines()))
                if len(r.rows) > self.max_rows:
                    print(f"       ... and {len(r.rows) - self.max_rows} more row(s)")


# --------------------------------------------------------------------------- #
# Payload clauses                                                              #
# --------------------------------------------------------------------------- #

def load_payload(path: Path) -> pd.DataFrame:
    # Read every value as text so the contract, not pandas inference, decides types.
    df = pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])
    df.index = pd.RangeIndex(2, len(df) + 2, name="csv_line")  # line 1 is the header
    return df


def parse(values: pd.Series, col_type: str) -> pd.Series:
    """Parsed values; NaN/NaT where a non-null value does not parse as col_type."""
    if col_type == "date":
        return pd.to_datetime(values, format="%Y-%m-%d", errors="coerce")
    if col_type in ("decimal", "integer"):
        nums = pd.to_numeric(values, errors="coerce")
        nums = nums.where(nums.abs() != float("inf"))
        if col_type == "integer":
            nums = nums.where(nums.isna() | (nums % 1 == 0))
        return nums
    if col_type == "boolean":
        return values.str.lower().map({"true": True, "false": False})
    return values


def check_dataset(ds: Dataset, payload_dir: Path, report: Report) -> pd.DataFrame | None:
    path = payload_dir / ds.file
    report.add(f"{ds.name}: file {ds.file} delivered", path.exists(), "" if path.exists() else f"not found in {payload_dir}")
    if not path.exists():
        return None
    df = load_payload(path)

    declared = [c.name for c in ds.columns]
    missing = [c for c in declared if c not in df.columns]
    report.add(f"{ds.name}: all declared columns present", not missing, f"missing {missing}" if missing else "")
    if not ds.allow_undeclared_columns:
        extra = [c for c in df.columns if c not in declared]
        report.add(f"{ds.name}: no undeclared columns", not extra, f"undeclared {extra}" if extra else "")

    for col in ds.columns:
        if col.name not in df.columns:
            report.skip(f"{ds.name}.{col.name}: column checks", "column missing")
            continue
        raw = df[col.name]
        present = raw.notna()
        parsed = parse(raw, col.type)
        clause = f"{ds.name}.{col.name}"

        report.check(f"{clause}: type {col.type}", df[present & parsed.isna()],
                     f"do not parse as {col.type}")
        if not col.nullable:
            report.check(f"{clause}: not null", df[~present], "are null")
        if col.pattern:
            bad = present & ~raw.fillna("").str.fullmatch(col.pattern)
            report.check(f"{clause}: matches {col.pattern}", df[bad], "do not match the pattern")
        if col.allowed_values is not None:
            bad = present & ~raw.isin(col.allowed_values)
            report.check(f"{clause}: in {col.allowed_values}", df[bad], "hold a value outside the allowed set")
        if col.range:
            lo, hi = col.range.min, col.range.max
            bad = parsed.notna() & (
                (parsed < lo if lo is not None else False) | (parsed > hi if hi is not None else False)
            )
            report.check(f"{clause}: within [{lo}, {hi}]", df[bad], "fall outside the allowed range")

    keys = ds.primary_key
    if all(k in df.columns for k in keys):
        dupes = df[df.duplicated(keys, keep=False)].sort_values(keys)
        report.check(f"{ds.name}: primary key ({', '.join(keys)}) unique", dupes, "share a key value with another row")
    return df


def check_freshness(contract: Contract, frames: dict[str, pd.DataFrame], now: datetime, report: Report) -> None:
    f = contract.sla.freshness
    clause = f"sla: freshness of {f.dataset}.{f.column} <= {f.max_age_hours:g}h"
    df = frames.get(f.dataset)
    if df is None or f.column not in df.columns:
        report.skip(clause, f"{f.dataset}.{f.column} not available")
        return
    newest = parse(df[f.column], "date").max()
    if pd.isna(newest):
        report.add(clause, False, f"no parseable {f.column} values")
        return
    tz = ZoneInfo(f.timezone)
    # A business date's data is complete at the end of that day (local midnight).
    complete_at = datetime.combine(newest.date() + timedelta(days=1), datetime.min.time(), tzinfo=tz)
    age_hours = (now.astimezone(tz) - complete_at).total_seconds() / 3600
    detail = f"newest {f.column} {newest.date()}, {age_hours:.1f}h old at {now.astimezone(tz):%Y-%m-%d %H:%M %Z}"
    if age_hours < -24:
        report.add(clause, False, detail + " (business date is in the future)")
    else:
        report.add(clause, age_hours <= f.max_age_hours, detail)


# --------------------------------------------------------------------------- #
# Breaking-change policy                                                       #
# --------------------------------------------------------------------------- #

@dataclass
class Change:
    kind: str
    where: str


def diff_contracts(old: Contract, new: Contract) -> list[Change]:
    changes: list[Change] = []
    old_ds = {d.name: d for d in old.datasets}
    new_ds = {d.name: d for d in new.datasets}
    changes += [Change("remove_dataset", n) for n in old_ds.keys() - new_ds.keys()]
    changes += [Change("add_dataset", n) for n in new_ds.keys() - old_ds.keys()]

    for name in old_ds.keys() & new_ds.keys():
        o, n = old_ds[name], new_ds[name]
        if o.primary_key != n.primary_key:
            changes.append(Change("change_primary_key", f"{name}: {o.primary_key} -> {n.primary_key}"))
        if o.allow_undeclared_columns != n.allow_undeclared_columns:
            kind = "allow_undeclared_columns" if n.allow_undeclared_columns else "disallow_undeclared_columns"
            changes.append(Change(kind, name))
        o_cols = {c.name: c for c in o.columns}
        n_cols = {c.name: c for c in n.columns}
        changes += [Change("remove_column", f"{name}.{c}") for c in o_cols.keys() - n_cols.keys()]
        changes += [Change("add_column", f"{name}.{c}") for c in n_cols.keys() - o_cols.keys()]
        for c in o_cols.keys() & n_cols.keys():
            changes += diff_column(f"{name}.{c}", o_cols[c], n_cols[c])

    o_age, n_age = old.sla.freshness.max_age_hours, new.sla.freshness.max_age_hours
    if n_age > o_age:
        changes.append(Change("relax_freshness_sla", f"{o_age:g}h -> {n_age:g}h"))
    elif n_age < o_age:
        changes.append(Change("tighten_freshness_sla", f"{o_age:g}h -> {n_age:g}h"))
    return sorted(changes, key=lambda c: (c.kind, c.where))


def diff_column(where: str, o: Column, n: Column) -> list[Change]:
    changes = []
    if o.type != n.type:
        changes.append(Change("change_column_type", f"{where}: {o.type} -> {n.type}"))
    if o.pattern != n.pattern:
        changes.append(Change("change_column_pattern", f"{where}: {o.pattern} -> {n.pattern}"))
    if o.nullable != n.nullable:
        changes.append(Change("make_column_nullable" if n.nullable else "make_column_required", where))
    if o.allowed_values is not None or n.allowed_values is not None:
        o_vals, n_vals = set(o.allowed_values or []), set(n.allowed_values or [])
        if o.allowed_values is not None and n.allowed_values is None:
            changes.append(Change("remove_allowed_value", f"{where}: value restriction dropped"))
        else:
            changes += [Change("remove_allowed_value", f"{where}: {v}") for v in sorted(o_vals - n_vals)]
            changes += [Change("add_allowed_value", f"{where}: {v}") for v in sorted(n_vals - o_vals)]
    if o.range != n.range:
        o_lo, o_hi = (o.range.min, o.range.max) if o.range else (None, None)
        n_lo, n_hi = (n.range.min, n.range.max) if n.range else (None, None)
        widened = (o_lo is not None and (n_lo is None or n_lo < o_lo)) or (o_hi is not None and (n_hi is None or n_hi > o_hi))
        changes.append(Change("widen_range" if widened else "narrow_range", f"{where}: [{o_lo}, {o_hi}] -> [{n_lo}, {n_hi}]"))
    return changes


def semver(v: str) -> tuple[int, int, int]:
    major, minor, patch = (int(p) for p in v.split("."))
    return major, minor, patch


def check_change_policy(baseline: Contract | None, contract: Contract, today: date, report: Report) -> None:
    if baseline is None:
        report.skip("change policy: breaking changes announced", "no --baseline contract given")
        return
    if baseline.contract.id != contract.contract.id:
        report.add("change policy: same contract id as baseline", False,
                   f"{baseline.contract.id} vs {contract.contract.id}")
        return

    # The policy in force is the baseline's: a producer cannot loosen the rules
    # in the same change that relies on the looser rules.
    policy = baseline.change_policy
    changes = diff_contracts(baseline, contract)
    old_v, new_v = baseline.contract.version, contract.contract.version
    rows = pd.DataFrame(
        [{"change": c.kind, "where": c.where,
          "classified": "breaking" if c.kind not in policy.non_breaking_changes else "non-breaking"}
         for c in changes],
        columns=["change", "where", "classified"],
    )
    # Anything the policy does not explicitly call non-breaking is treated as breaking.
    breaking = rows[rows["classified"] == "breaking"]
    print(f"Changes since baseline v{old_v}: {len(rows)} ({len(breaking)} breaking)")

    if rows.empty:
        report.add("change policy: contract unchanged or version-only change", True)
        return

    report.add(f"change policy: version bumped (v{old_v} -> v{new_v})", semver(new_v) > semver(old_v),
               "" if semver(new_v) > semver(old_v) else "contract changed without a version bump")
    if breaking.empty:
        report.add("change policy: no breaking changes", True)
        return

    report.check("change policy: breaking changes require a major version bump",
                 breaking if semver(new_v)[0] <= semver(old_v)[0] else breaking.iloc[0:0],
                 f"are breaking but v{new_v} is not a new major version of v{old_v}")

    window = policy.notification_window_days
    entry = contract.change_policy.entry(new_v)
    clause = f"change policy: v{new_v} announced >= {window} days before effective"
    if entry is None or not entry.breaking:
        report.check(clause, breaking, f"are breaking but v{new_v} has no breaking change_log notice")
        return
    notice = (entry.effective_on - entry.announced_on).days
    report.add(clause, notice >= window,
               f"announced {entry.announced_on}, effective {entry.effective_on} ({notice} days notice)")
    report.add(f"change policy: v{new_v} not enforced before effective date {entry.effective_on}",
               today >= entry.effective_on, f"validated on {today}")


# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Validate a data payload against a data contract.")
    ap.add_argument("contract", type=Path, help="contract YAML file")
    ap.add_argument("payload", type=Path, help="directory holding the dataset files named in the contract")
    ap.add_argument("--baseline", type=Path, help="previous contract version, to enforce the change policy")
    ap.add_argument("--now", help="validation time as ISO 8601 (default: now); naive times use the SLA timezone")
    ap.add_argument("--max-rows", type=int, default=20, help="violating rows to print per clause")
    args = ap.parse_args(argv)

    try:
        contract = load_contract(args.contract)
        baseline = load_contract(args.baseline) if args.baseline else None
    except (OSError, yaml.YAMLError, ValidationError) as e:
        print(f"CONTRACT INVALID: {e}", file=sys.stderr)
        return EXIT_UNUSABLE
    if not args.payload.is_dir():
        print(f"PAYLOAD NOT FOUND: {args.payload} is not a directory", file=sys.stderr)
        return EXIT_UNUSABLE

    tz = ZoneInfo(contract.sla.freshness.timezone)
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)

    meta, owner = contract.contract, contract.owner
    print(f"Contract {meta.id} v{meta.version} ({meta.status}), owner {owner.team}")
    print(f"Payload  {args.payload}   validated at {now.astimezone(tz):%Y-%m-%d %H:%M %Z}\n")

    report = Report(args.max_rows)
    frames = {}
    for ds in contract.datasets:
        df = check_dataset(ds, args.payload, report)
        if df is not None:
            frames[ds.name] = df
    check_freshness(contract, frames, now, report)
    check_change_policy(baseline, contract, now.astimezone(tz).date(), report)
    report.print()

    failed = report.failures
    passed = sum(r.status == "PASS" for r in report.results)
    skipped = sum(r.status == "SKIP" for r in report.results)
    print(f"\n{len(report.results)} clauses: {passed} passed, {len(failed)} failed, {skipped} skipped")
    if failed:
        print(f"CONTRACT VIOLATED: {len(failed)} clause(s) failed. "
              f"Producer contact: {owner.team} <{owner.email}>, {owner.slack}")
        return EXIT_VIOLATIONS
    print("CONTRACT SATISFIED")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
