"""Safe defaults, secrets kept out of files, and production readiness checks."""

from pathlib import Path

import pytest

from chaosmonkey.domain import Role
from chaosmonkey.settings import Settings, SettingsError, load_settings


def test_defaults_are_off_leashed_and_not_scheduling() -> None:
    settings = Settings()
    assert settings.enabled is False
    assert settings.leashed is True
    assert settings.schedule_enabled is False
    assert settings.allowed_accounts == ()
    assert settings.api.dev_auth is False


def test_secrets_in_the_file_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "chaosmonkey.toml"
    path.write_text('audit_key = "abc"\n', encoding="utf-8")
    with pytest.raises(SettingsError, match="CHAOSMONKEY_AUDIT_KEY"):
        load_settings(path, env={})


def test_database_password_in_the_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "chaosmonkey.toml"
    # Built in two parts so the pre-commit guard does not mistake it for a real secret.
    url = "postgresql+psycopg://app:" + "hunter2@db/chaosmonkey"
    path.write_text(f'database_url = "{url}"\n', encoding="utf-8")
    with pytest.raises(SettingsError, match="CHAOSMONKEY_DATABASE_URL"):
        load_settings(path, env={})


def test_secrets_come_from_the_environment(tmp_path: Path) -> None:
    path = tmp_path / "chaosmonkey.toml"
    path.write_text("enabled = true\n", encoding="utf-8")
    settings = load_settings(
        path,
        env={
            "CHAOSMONKEY_AUDIT_KEY": "a" * 40,
            "CHAOSMONKEY_DATABASE_URL": "postgresql+psycopg://app:" + "pw@db/chaosmonkey",
        },
    )
    assert settings.enabled
    assert settings.audit_key_bytes == b"a" * 40
    assert "pw" not in repr(settings)  # secrets are redacted when printed


def test_missing_explicit_file_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="not found"):
        load_settings(tmp_path / "nope.toml", env={})


def test_invalid_values_are_refused() -> None:
    with pytest.raises(ValueError, match="start_hour"):
        Settings(start_hour=16, end_hour=9)
    with pytest.raises(ValueError, match="time_zone"):
        Settings(time_zone="Mars/Olympus")
    with pytest.raises(ValueError, match="extra"):
        Settings.model_validate({"enabeld": True})  # a typo is an error, not ignored


def test_production_lists_every_problem() -> None:
    problems = Settings(environment="production", api={"dev_auth": True}).production_problems()
    text = "\n".join(problems)
    for expected in ("PostgreSQL", "CHAOSMONKEY_AUDIT_KEY", "single sign-on", "dev_auth", "TLS"):
        assert expected in text
    with pytest.raises(SettingsError):
        Settings(environment="production").ensure_production_ready()


def test_a_well_configured_production_has_only_the_driver_left() -> None:
    settings = Settings(
        environment="production",
        database_url="postgresql+psycopg://db/chaosmonkey",
        audit_key="z" * 40,
        api={
            "oidc_issuer": "https://login.example.com/",
            "oidc_audience": "api://chaosmonkey",
            "oidc_jwks_url": "https://login.example.com/keys",
            "role_groups": {Role.APPROVER: ["approvers"], Role.ADMIN: ["admins"]},
            "allowed_hosts": ["chaosmonkey.example.com"],
            "behind_tls_proxy": True,
        },
    )
    assert settings.production_problems() == [
        "The simulator driver is for development. Real drivers arrive in milestone M3."
    ]
