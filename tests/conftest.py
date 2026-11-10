"""Shared test fixtures. All fleets here are synthetic; nothing real is touched."""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from chaosmonkey.clock import FixedClock
from chaosmonkey.domain import Actor, AppConfig, Mode, Role
from chaosmonkey.runtime import Runtime
from chaosmonkey.service import ChaosMonkey
from chaosmonkey.settings import Settings
from chaosmonkey.views import AppView

# Monday 9 November 2026, before working hours (09:00-17:00 UTC in tests).
MONDAY_EARLY = datetime(2026, 11, 9, 8, 0, tzinfo=UTC)

FLEET = """
[[group]]
app = "payments-api"
account = "prod"
region = "r1"
stack = "main"
replicas = 4
recovery_seconds = 60

[[group]]
app = "statements"
account = "prod"
region = "r1"
replicas = 3
recovers = false

[[group]]
app = "kyc"
account = "prod"
region = "r1"
replicas = 2

[[group]]
app = "ledger"
account = "prod"
region = "r1"
stack = "primary"
replicas = 3
protected = true

[[group]]
app = "sandbox"
account = "unlisted"
region = "r1"
replicas = 5
"""


def make_actor(subject: str, *roles: Role, teams: tuple[str, ...] = ()) -> Actor:
    return Actor(
        subject=subject, display_name=subject, roles=frozenset(roles), teams=frozenset(teams)
    )


OWNER = make_actor(
    "olivia", Role.APP_OWNER, teams=("payments", "platform", "risk", "onboarding", "ledger")
)
OTHER_OWNER = make_actor("oscar", Role.APP_OWNER, teams=("marketing",))
APPROVER = make_actor("arjun", Role.APPROVER)
SECOND_APPROVER = make_actor("bea", Role.APPROVER)
OPERATOR = make_actor("omar", Role.OPERATOR)
ADMIN = make_actor("ada", Role.ADMIN)
VIEWER = make_actor("vik", Role.VIEWER)


@pytest.fixture
def fleet_file(tmp_path: Path) -> Path:
    path = tmp_path / "fleet.toml"
    path.write_text(FLEET, encoding="utf-8")
    return path


@pytest.fixture
def make_settings(tmp_path: Path, fleet_file: Path) -> Callable[..., Settings]:
    def build(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "enabled": True,
            "leashed": False,
            "schedule_enabled": True,
            "time_zone": "UTC",
            "start_hour": 9,
            "end_hour": 17,
            "allowed_accounts": ("prod",),
            "max_kills_per_day": 10,
            "max_concurrent_kills": 5,
            "recovery_timeout_minutes": 10,
            "missed_kill_grace_minutes": 600,
            "simulator_fleet": fleet_file,
            "simulator_state": tmp_path / "sim-state.json",
            "database_url": f"sqlite:///{(tmp_path / 'chaosmonkey.db').as_posix()}",
            "dev_signing_key": "test-dev-signing-key-0123456789abcdef",
            "api": {"allowed_hosts": ["testserver"], "dev_auth": True, "run_executor": False},
        }
        api_overrides = overrides.pop("api", {})
        values.update(overrides)
        values["api"] = {**values["api"], **api_overrides}
        return Settings.model_validate(values)

    return build


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(MONDAY_EARLY)


@pytest.fixture
def make_runtime(
    make_settings: Callable[..., Settings], clock: FixedClock
) -> Callable[..., Runtime]:
    def build(**overrides: Any) -> Runtime:
        return Runtime(make_settings(**overrides), clock)

    return build


@pytest.fixture
def runtime(make_runtime: Callable[..., Runtime]) -> Runtime:
    return make_runtime()


def register(service: ChaosMonkey, name: str, team: str = "payments", **fields: Any) -> AppView:
    config = {"name": name, "owner_team": team, "enabled": True, "mean_days_between_kills": 1}
    config.update(fields)
    return service.save_app(OWNER, AppConfig.model_validate(config))


def go_live(service: ChaosMonkey, name: str, team: str = "payments", **fields: Any) -> AppView:
    register(service, name, team, mode=Mode.LIVE, **fields)
    request = service.request_approval(OWNER, name, "Planned resilience test with on-call ready")
    service.decide_approval(APPROVER, request.id, approve=True, note="Runbook reviewed")
    return service.get_app(VIEWER, name)
