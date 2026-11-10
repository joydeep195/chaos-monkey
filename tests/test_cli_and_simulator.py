"""The command line and the simulated fleet."""

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chaosmonkey.cli import app
from chaosmonkey.clock import FixedClock
from chaosmonkey.domain import Target
from chaosmonkey.drivers.base import DriverError
from chaosmonkey.drivers.simulator import SimulatorDriver
from tests.conftest import FLEET, MONDAY_EARLY

runner = CliRunner()


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Path]:
    monkeypatch.setenv("CHAOSMONKEY_DEV_SIGNING_KEY", "cli-test-key-0123456789abcdefghij")
    (tmp_path / "fleet.toml").write_text(FLEET, encoding="utf-8")

    def write(environment: str = "development", dev_auth: bool = True) -> Path:
        path = tmp_path / "chaosmonkey.toml"
        path.write_text(
            "\n".join(
                [
                    f'environment = "{environment}"',
                    f'database_url = "sqlite:///{(tmp_path / "cli.db").as_posix()}"',
                    f'simulator_fleet = "{(tmp_path / "fleet.toml").as_posix()}"',
                    f'simulator_state = "{(tmp_path / "state.json").as_posix()}"',
                    'allowed_accounts = ["prod"]',
                    "[api]",
                    f"dev_auth = {'true' if dev_auth else 'false'}",
                ]
            ),
            encoding="utf-8",
        )
        return path

    return write


def cli(path: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(app, ["--config", str(path), *args])
    return result.exit_code, result.output


def test_cli_maker_checker_flow(config: Callable[..., Path]) -> None:
    path = config()
    assert cli(path, "init")[0] == 0
    code, out = cli(
        path,
        "apps",
        "set",
        "payments-api",
        "--team",
        "payments",
        "--enable",
        "--mode",
        "live",
        "--except",
        "*/*/canary/*",
    )
    assert code == 0, out
    assert "version 1" in out
    code, out = cli(
        path,
        "--as",
        "olivia",
        "approvals",
        "request",
        "payments-api",
        "--reason",
        "Quarterly resilience test",
    )
    assert code == 0, out
    code, _ = cli(path, "--as", "olivia", "approvals", "approve", "1", "--note", "self")
    assert code != 0
    code, out = cli(path, "--as", "arjun", "approvals", "approve", "1", "--note", "Runbook ok")
    assert code == 0, out
    code, out = cli(path, "apps", "show", "payments-api")
    assert '"canary"' in out
    code, out = cli(path, "audit", "verify")
    assert code == 0
    assert "intact" in out


@pytest.mark.parametrize("start", ["2026-11-30", "2026-11-30T09:00", "2026-11-30 09:00:00"])
def test_cli_freeze_window_accepts_common_time_formats(
    config: Callable[..., Path], start: str
) -> None:
    path = config()
    code, out = cli(
        path, "freeze", "add", "Month-end close", "--start", start, "--end", "2026-12-02T18:00"
    )
    assert code == 0, out
    code, out = cli(path, "freeze", "list")
    assert "Month-end close" in out


def test_cli_disabled_app_says_it_is_off(config: Callable[..., Path]) -> None:
    path = config()
    code, out = cli(path, "apps", "set", "payments-api", "--team", "payments", "--disable")
    assert code == 0, out
    assert "Off: the app is disabled" in out


def test_cli_fleet_and_once(config: Callable[..., Path]) -> None:
    path = config()
    code, out = cli(path, "fleet")
    assert code == 0
    assert "payments-api-main" in out
    code, out = cli(path, "run", "--once")
    assert code == 0, out


def test_production_cli_only_reads_and_breaks_glass(config: Callable[..., Path]) -> None:
    path = config(environment="production", dev_auth=False)
    code, out = cli(path, "apps", "set", "x-app", "--team", "payments")
    assert code == 1
    assert "through the API" in out
    code, out = cli(path, "kill-switch", "engage", "--reason", "Suspected breach")
    assert code == 0, out
    code, out = cli(path, "kill-switch", "status")
    assert "Suspected breach" in out
    code, out = cli(path, "--as", "someone-else", "kill-switch", "status")
    assert code == 1
    code, out = cli(path, "run", "--once")
    assert code != 0  # refuses to run until production checks pass


def test_dev_token_needs_dev_auth(config: Callable[..., Path]) -> None:
    code, out = cli(config(dev_auth=False), "dev-token", "--subject", "a", "--role", "viewer")
    assert code == 1
    code, out = cli(config(), "dev-token", "--subject", "a", "--role", "viewer")
    assert code == 0
    assert out.count(".") == 2  # a JWT


def test_simulator_replaces_instances_after_recovery_time(fleet_file: Path, tmp_path: Path) -> None:
    clock = FixedClock(MONDAY_EARLY)
    driver = SimulatorDriver(fleet_file, tmp_path / "state.json", clock)
    victim = next(t for t in driver.list_targets() if t.app == "payments-api")
    driver.terminate(victim)
    payments = [t for t in driver.list_targets() if t.app == "payments-api"]
    assert len(payments) == 3
    assert victim.id not in {t.id for t in payments}
    clock.set(MONDAY_EARLY + timedelta(seconds=61))
    payments = [t for t in driver.list_targets() if t.app == "payments-api"]
    assert len(payments) == 4
    assert any(t.id.endswith("-005") for t in payments)


def test_simulator_fragile_group_never_recovers(fleet_file: Path, tmp_path: Path) -> None:
    clock = FixedClock(MONDAY_EARLY)
    driver = SimulatorDriver(fleet_file, tmp_path / "state.json", clock)
    victim = next(t for t in driver.list_targets() if t.app == "statements")
    driver.terminate(victim)
    clock.set(MONDAY_EARLY + timedelta(days=1))
    assert sum(1 for t in driver.list_targets() if t.app == "statements") == 2


def test_simulator_refuses_unknown_instances(fleet_file: Path, tmp_path: Path) -> None:
    driver = SimulatorDriver(fleet_file, tmp_path / "state.json", FixedClock(MONDAY_EARLY))
    with pytest.raises(DriverError):
        driver.terminate(Target("ghost-1", "payments-api", "prod", "r1", "main"))


def test_simulator_rejects_duplicate_groups(tmp_path: Path) -> None:
    fleet = tmp_path / "dup.toml"
    group = '[[group]]\napp = "a"\naccount = "p"\nregion = "r"\nreplicas = 1\n'
    fleet.write_text(group * 2, encoding="utf-8")
    with pytest.raises(DriverError, match="twice"):
        SimulatorDriver(fleet, tmp_path / "s.json", FixedClock(MONDAY_EARLY))
