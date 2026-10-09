"""Run the data contract (contracts/accounts.yaml) as part of the test gate.

Three checks, each mapping to one way the producer can break consumers:
  * the contract file itself is well formed,
  * the delivered payload satisfies every contract clause,
  * any edit to the contract obeys the breaking-change policy relative to the
    version in force (the contract on the baseline git ref).
"""

from __future__ import annotations

import subprocess

import pytest

import validate_contract as vc
from tests.conftest import REPO_ROOT

CONTRACT = REPO_ROOT / "contracts" / "accounts.yaml"
CONTRACT_GIT_PATH = "contracts/accounts.yaml"


def setting(config: pytest.Config, name: str) -> str:
    """Command-line override first, then pytest.ini."""
    return config.getoption(f"--{name.replace('_', '-')}") or config.getini(name)


def test_contract_is_well_formed():
    try:
        contract = vc.load_contract(CONTRACT)
    except vc.ValidationError as err:
        print(err)
        pytest.fail(f"{CONTRACT.name} is not a valid contract", pytrace=False)
    print(f"{contract.contract.id} v{contract.contract.version}, owner {contract.owner.team}")


def test_payload_satisfies_contract(request, data_dir):
    argv = [str(CONTRACT), str(data_dir)]
    validation_time = setting(request.config, "validation_time")
    if validation_time:
        argv += ["--now", validation_time]

    # The validator prints its full per-clause report; pytest shows it on failure.
    exit_code = vc.main(argv)
    assert exit_code == vc.EXIT_OK, f"validate_contract.py exited {exit_code}: payload violates {CONTRACT.name}"


def test_contract_change_follows_policy(request, tmp_path):
    ref = setting(request.config, "contract_baseline_ref")
    shown = subprocess.run(
        ["git", "show", f"{ref}:{CONTRACT_GIT_PATH}"],
        cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    if shown.returncode != 0:
        pytest.skip(f"no baseline contract at {ref}:{CONTRACT_GIT_PATH} (new contract, or ref not fetched)")
    baseline_file = tmp_path / "baseline.yaml"
    baseline_file.write_text(shown.stdout, encoding="utf-8")

    baseline, contract = vc.load_contract(baseline_file), vc.load_contract(CONTRACT)
    report = vc.Report(max_rows=50)
    tz = vc.ZoneInfo(contract.sla.freshness.timezone)
    today = vc.resolve_now(setting(request.config, "validation_time"), tz).date()

    print(f"baseline: {ref} (v{baseline.contract.version}); working tree: v{contract.contract.version}")
    vc.check_change_policy(baseline, contract, today, report)
    report.print()
    failed = [r.clause for r in report.failures]
    assert not failed, f"contract change violates the breaking-change policy: {failed}"
