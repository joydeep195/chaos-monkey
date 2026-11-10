"""Settings, read from chaosmonkey.toml. Secrets come only from environment variables.

Defaults are the safe ones: Chaos Monkey is switched off, leashed (dry run) and not
scheduling, exactly as Netflix ships the original.
"""

import os
import tomllib
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from chaosmonkey.domain import Role

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAY_NUMBERS: dict[str, int] = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}
# Only asymmetric algorithms: the identity provider signs, Chaos Monkey only verifies.
SigningAlgorithm = Literal[
    "RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"
]

SECRET_ENVIRONMENT_VARIABLES = {
    "database_url": "CHAOSMONKEY_DATABASE_URL",
    "audit_key": "CHAOSMONKEY_AUDIT_KEY",
    "dev_signing_key": "CHAOSMONKEY_DEV_SIGNING_KEY",
}
MIN_AUDIT_KEY_LENGTH = 32


class SettingsError(Exception):
    """The settings cannot be used. The message says what to change."""


class ApiSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)

    # Single sign-on (OpenID Connect). All three are required in production.
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    allowed_algorithms: tuple[SigningAlgorithm, ...] = ("RS256", "ES256")
    # The claim that names the person in the audit log ("sub", "email" or
    # "preferred_username", depending on the identity provider).
    subject_claim: str = "sub"
    # The token claim listing the person's identity-provider groups.
    groups_claim: str = "groups"
    # Which identity-provider groups grant which role.
    role_groups: dict[Role, tuple[str, ...]] = Field(default_factory=dict)
    # Groups starting with this prefix name the person's teams: "team-payments" is "payments".
    team_group_prefix: str = "team-"
    # Local development tokens (see `chaosmonkey dev-token`). Refused in production.
    dev_auth: bool = False

    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1")
    cors_origins: tuple[str, ...] = ()
    rate_limit_per_minute: int = Field(default=120, ge=1, le=100_000)
    max_body_bytes: int = Field(default=65_536, ge=1_024, le=10_485_760)

    tls_cert_file: Path | None = None
    tls_key_file: Path | None = None
    # Set when a load balancer or ingress terminates TLS in front of Chaos Monkey.
    behind_tls_proxy: bool = False

    # Run the scheduler and executor inside the API process.
    run_executor: bool = True


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    environment: Literal["development", "production"] = "development"

    # Master switches, named and defaulted as in Netflix's original.
    enabled: bool = False
    leashed: bool = True
    schedule_enabled: bool = False

    # Working hours: kills happen only inside this window on working days.
    time_zone: str = "UTC"
    start_hour: int = Field(default=9, ge=0, le=23)
    end_hour: int = Field(default=15, ge=1, le=24)
    working_days: tuple[Weekday, ...] = ("mon", "tue", "wed", "thu", "fri")
    holidays: tuple[date, ...] = ()

    # Global guard rails.
    allowed_accounts: tuple[str, ...] = ()
    max_kills_per_day: int = Field(default=10, ge=0, le=10_000)
    max_concurrent_kills: int = Field(default=1, ge=1, le=100)
    recovery_timeout_minutes: int = Field(default=10, ge=1, le=1_440)
    missed_kill_grace_minutes: int = Field(default=30, ge=1, le=600)
    approval_valid_days: int = Field(default=90, ge=1, le=366)
    tick_seconds: int = Field(default=30, ge=5, le=3_600)

    driver: Literal["simulator"] = "simulator"
    simulator_fleet: Path = Path("fleet.toml")
    simulator_state: Path = Path("data/simulator-state.json")

    # Secrets: filled from environment variables only (see load_settings).
    database_url: SecretStr = SecretStr("sqlite:///data/chaosmonkey.db")
    audit_key: SecretStr | None = None
    dev_signing_key: SecretStr | None = None

    api: ApiSettings = Field(default_factory=ApiSettings)

    @model_validator(mode="after")
    def _check(self) -> Self:
        try:
            ZoneInfo(self.time_zone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(f"Unknown time_zone {self.time_zone!r}") from error
        if self.start_hour >= self.end_hour:
            raise ValueError("start_hour must be earlier than end_hour")
        if not self.working_days:
            raise ValueError("working_days cannot be empty")
        try:
            make_url(self.database_url.get_secret_value())
        except ArgumentError as error:
            raise ValueError("database_url is not a valid database URL") from error
        return self

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.time_zone)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def audit_key_bytes(self) -> bytes | None:
        return self.audit_key.get_secret_value().encode() if self.audit_key else None

    def production_problems(self) -> list[str]:
        """Everything that must be fixed before Chaos Monkey may run in production."""
        problems: list[str] = []
        url = make_url(self.database_url.get_secret_value())
        if url.get_backend_name() == "sqlite":
            problems.append("Use PostgreSQL in production; SQLite is for a single machine.")
        key = self.audit_key.get_secret_value() if self.audit_key else ""
        if len(key) < MIN_AUDIT_KEY_LENGTH:
            problems.append(
                "Set CHAOSMONKEY_AUDIT_KEY to a random value of at least 32 characters, so the "
                "audit log cannot be re-signed by someone with database access."
            )
        api = self.api
        if not (api.oidc_issuer and api.oidc_audience and api.oidc_jwks_url):
            problems.append(
                "Configure single sign-on: api.oidc_issuer, api.oidc_audience and "
                "api.oidc_jwks_url."
            )
        elif not api.oidc_jwks_url.startswith("https://"):
            problems.append("api.oidc_jwks_url must use https.")
        if api.dev_auth:
            problems.append("api.dev_auth must be false in production.")
        if Role.APPROVER not in api.role_groups or Role.ADMIN not in api.role_groups:
            problems.append(
                "Map identity-provider groups to at least the approver and admin roles in "
                "api.role_groups."
            )
        if "*" in api.allowed_hosts or not api.allowed_hosts:
            problems.append("List the exact host names in api.allowed_hosts (no wildcard).")
        if "*" in api.cors_origins:
            problems.append("api.cors_origins cannot contain a wildcard.")
        if not (api.tls_cert_file and api.tls_key_file) and not api.behind_tls_proxy:
            problems.append(
                "Serve over TLS: set api.tls_cert_file and api.tls_key_file, or set "
                "api.behind_tls_proxy = true when a load balancer terminates TLS."
            )
        if self.driver == "simulator":
            problems.append(
                "The simulator driver is for development. Real drivers arrive in milestone M3."
            )
        return problems

    def ensure_production_ready(self) -> None:
        if not self.is_production:
            return
        problems = self.production_problems()
        if problems:
            lines = "\n".join(f"  - {problem}" for problem in problems)
            raise SettingsError(f"Chaos Monkey is not ready for production:\n{lines}")


def load_settings(path: Path | None = None, env: Mapping[str, str] | None = None) -> Settings:
    """Read settings from a TOML file (if present) and secrets from the environment."""
    env = os.environ if env is None else env
    explicit = path is not None or "CHAOSMONKEY_CONFIG" in env
    if path is None:
        path = Path(env.get("CHAOSMONKEY_CONFIG", "chaosmonkey.toml"))

    data: dict[str, object] = {}
    if path.is_file():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as error:
            raise SettingsError(f"{path} is not valid TOML: {error}") from error
    elif explicit:
        raise SettingsError(f"Config file not found: {path}")

    for key in ("audit_key", "dev_signing_key"):
        if key in data:
            raise SettingsError(
                f"{key} is a secret. Set it in the {SECRET_ENVIRONMENT_VARIABLES[key]} "
                f"environment variable, not in {path.name}."
            )
    file_url = data.get("database_url")
    if isinstance(file_url, str) and _has_password(file_url):
        raise SettingsError(
            f"database_url in {path.name} contains a password. Put the whole URL in the "
            "CHAOSMONKEY_DATABASE_URL environment variable instead."
        )
    for key, variable in SECRET_ENVIRONMENT_VARIABLES.items():
        if env.get(variable):
            data[key] = env[variable]

    try:
        return Settings.model_validate(data)
    except ValidationError as error:
        raise SettingsError(f"Invalid settings in {path}:\n{error}") from error


def _has_password(url: str) -> bool:
    try:
        return make_url(url).password is not None
    except ArgumentError:
        return False
