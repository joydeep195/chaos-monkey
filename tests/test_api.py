"""The HTTP API: sign-in, roles, hardening headers, limits and error handling."""

import base64
import hashlib
import hmac
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from chaosmonkey.api.app import create_api
from chaosmonkey.api.auth import TokenVerifier, issue_dev_token
from chaosmonkey.domain import Role
from chaosmonkey.runtime import Runtime
from tests.conftest import OPERATOR as OPERATOR_ACTOR

ISSUER = "https://login.example.com/"
AUDIENCE = "api://chaosmonkey"
APP_BODY = {
    "config": {
        "name": "payments-api",
        "owner_team": "payments",
        "enabled": True,
        "mode": "live",
        "mean_days_between_kills": 2,
    }
}


def token(runtime: Runtime, subject: str, *roles: Role, teams: tuple[str, ...] = ()) -> str:
    return issue_dev_token(
        runtime.settings, subject=subject, roles=roles, teams=teams, now=datetime.now(UTC)
    )


def auth(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


@pytest.fixture
def client(runtime: Runtime) -> Iterator[TestClient]:
    with TestClient(create_api(runtime)) as test_client:
        yield test_client


@pytest.fixture
def owner(runtime: Runtime) -> dict[str, str]:
    return auth(token(runtime, "olivia", Role.APP_OWNER, teams=("payments",)))


@pytest.fixture
def approver(runtime: Runtime) -> dict[str, str]:
    return auth(token(runtime, "arjun", Role.APPROVER))


def test_health_needs_no_sign_in(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}


def test_api_needs_sign_in(client: TestClient) -> None:
    response = client.get("/api/v1/apps")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert client.get("/api/v1/apps", headers=auth("not-a-token")).status_code == 401


def test_dev_token_signs_in(client: TestClient, owner: dict[str, str]) -> None:
    me = client.get("/api/v1/me", headers=owner).json()
    assert me == {
        "subject": "olivia",
        "display_name": "olivia",
        "roles": ["app_owner"],
        "teams": ["payments"],
    }


def test_expired_and_forged_tokens_are_refused(client: TestClient, runtime: Runtime) -> None:
    old = issue_dev_token(
        runtime.settings,
        subject="olivia",
        roles=[Role.ADMIN],
        teams=[],
        now=datetime.now(UTC) - timedelta(days=2),
    )
    response = client.get("/api/v1/me", headers=auth(old))
    assert response.status_code == 401
    assert "expired" in response.json()["message"]

    claims = jwt.decode(
        token(runtime, "olivia", Role.APP_OWNER), options={"verify_signature": False}
    )
    claims["chaosmonkey_roles"] = ["admin"]
    forged = jwt.encode(claims, "an-attacker-guessed-this-key-0123456789", algorithm="HS256")
    assert client.get("/api/v1/me", headers=auth(forged)).status_code == 401


def test_dev_tokens_are_refused_in_production(make_runtime: Callable[..., Runtime]) -> None:
    development = make_runtime()
    signed = token(development, "olivia", Role.ADMIN)
    production = make_runtime(environment="production", api={"dev_auth": True})
    with TestClient(create_api(production)) as client:
        assert client.get("/api/v1/me", headers=auth(signed)).status_code == 401


def test_full_maker_checker_flow(
    client: TestClient, owner: dict[str, str], approver: dict[str, str]
) -> None:
    saved = client.put("/api/v1/apps/payments-api", json=APP_BODY, headers=owner)
    assert saved.status_code == 200, saved.text
    assert saved.json()["effective_mode"] == "dry_run"

    request = client.post(
        "/api/v1/apps/payments-api/approval-requests",
        json={"reason": "Quarterly resilience test, on-call briefed"},
        headers=owner,
    )
    assert request.status_code == 201
    request_id = request.json()["id"]

    self_approval = client.post(
        f"/api/v1/approvals/{request_id}/approve", json={"note": "mine"}, headers=owner
    )
    assert self_approval.status_code == 403

    approved = client.post(
        f"/api/v1/approvals/{request_id}/approve", json={"note": "Runbook ok"}, headers=approver
    )
    assert approved.status_code == 200
    app = client.get("/api/v1/apps/payments-api", headers=owner).json()
    assert app["effective_mode"] == "live"
    assert app["approval"]["decided_by"] == "api:arjun"

    audit = client.get("/api/v1/audit", headers=owner).json()
    assert [e["action"] for e in audit] == [
        "app.created",
        "approval.requested",
        "approval.approved",
    ]
    assert client.get("/api/v1/audit/verify", headers=owner).json()["ok"] is True


def test_roles_and_teams_are_enforced(client: TestClient, runtime: Runtime) -> None:
    viewer = auth(token(runtime, "vik", Role.VIEWER))
    outsider = auth(token(runtime, "oscar", Role.APP_OWNER, teams=("marketing",)))
    assert client.put("/api/v1/apps/payments-api", json=APP_BODY, headers=viewer).status_code == 403
    response = client.put("/api/v1/apps/payments-api", json=APP_BODY, headers=outsider)
    assert response.status_code == 403
    assert response.json()["error"] == "forbidden"
    engage = client.post("/api/v1/kill-switch/engage", json={"reason": "drill"}, headers=viewer)
    assert engage.status_code == 403


def test_kill_switch_over_the_api(client: TestClient, runtime: Runtime) -> None:
    operator = auth(token(runtime, "omar", Role.OPERATOR))
    on = client.post(
        "/api/v1/kill-switch/engage", json={"reason": "Payments incident"}, headers=operator
    )
    assert on.json()["engaged"] is True
    assert on.json()["by"] == "api:omar"
    off = client.post("/api/v1/kill-switch/release", json={"reason": "Resolved"}, headers=operator)
    assert off.json()["engaged"] is False


def test_bad_input_is_refused(client: TestClient, owner: dict[str, str]) -> None:
    extra = {"config": {**APP_BODY["config"], "surprise": True}}
    assert client.put("/api/v1/apps/payments-api", json=extra, headers=owner).status_code == 422
    other_name = client.put("/api/v1/apps/other-app", json=APP_BODY, headers=owner)
    assert other_name.status_code == 422
    assert client.get("/api/v1/apps/Bad_Name", headers=owner).status_code == 422
    too_often = {"config": {**APP_BODY["config"], "mean_days_between_kills": 0.1}}
    assert client.put("/api/v1/apps/payments-api", json=too_often, headers=owner).status_code == 422


def test_stale_version_gets_409(client: TestClient, owner: dict[str, str]) -> None:
    client.put("/api/v1/apps/payments-api", json=APP_BODY, headers=owner)
    stale = {**APP_BODY, "expected_version": 7}
    response = client.put("/api/v1/apps/payments-api", json=stale, headers=owner)
    assert response.status_code == 409


def test_security_headers_on_every_response(client: TestClient, owner: dict[str, str]) -> None:
    for response in (
        client.get("/healthz"),
        client.get("/api/v1/apps"),
        client.get("/api/v1/apps/missing-app", headers=owner),
    ):
        headers = response.headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert "default-src 'none'" in headers["Content-Security-Policy"]
        assert headers["Cache-Control"] == "no-store"
        assert len(headers["X-Request-ID"]) == 32
        assert "server" not in headers


def test_production_hides_docs_and_sends_hsts(make_runtime: Callable[..., Runtime]) -> None:
    runtime = make_runtime(environment="production")
    with TestClient(create_api(runtime)) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        assert "max-age" in client.get("/healthz").headers["Strict-Transport-Security"]


def test_docs_available_in_development(client: TestClient) -> None:
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_rate_limit(make_runtime: Callable[..., Runtime]) -> None:
    runtime = make_runtime(api={"rate_limit_per_minute": 3})
    with TestClient(create_api(runtime)) as client:
        codes = [client.get("/api/v1/apps").status_code for _ in range(5)]
        assert codes[:3] == [401, 401, 401]
        assert codes[3:] == [429, 429]
        assert client.get("/healthz").status_code == 200  # health checks are exempt


def test_large_bodies_are_refused(client: TestClient, owner: dict[str, str]) -> None:
    huge = {"reason": "x" * 70_000}
    response = client.post("/api/v1/kill-switch/engage", json=huge, headers=owner)
    assert response.status_code == 413


def test_unknown_host_is_refused(client: TestClient) -> None:
    assert client.get("/healthz", headers={"Host": "evil.example.com"}).status_code == 400


def test_unexpected_errors_reveal_nothing(
    runtime: Runtime, owner: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("connection string postgres://secret@db")

    monkeypatch.setattr(runtime.service, "list_apps", explode)
    with TestClient(create_api(runtime), raise_server_exceptions=False) as client:
        response = client.get("/api/v1/apps", headers=owner)
    assert response.status_code == 500
    assert "secret" not in response.text
    assert response.json()["error"] == "internal_error"


# ----- single sign-on (OpenID Connect) ---------------------------------------------------


class FakeJwks:
    """Stands in for the identity provider's published keys."""

    def __init__(self, public_key: Any) -> None:
        self.public_key = public_key

    def get_signing_key_from_jwt(self, _token: str) -> SimpleNamespace:
        return SimpleNamespace(key=self.public_key)


@pytest.fixture
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def sso_client(
    make_runtime: Callable[..., Runtime], rsa_key: rsa.RSAPrivateKey
) -> Iterator[TestClient]:
    runtime = make_runtime(
        api={
            "dev_auth": False,
            "oidc_issuer": ISSUER,
            "oidc_audience": AUDIENCE,
            "oidc_jwks_url": "https://login.example.com/keys",
            "subject_claim": "email",
            "role_groups": {"approver": ["fl-approvers"], "app_owner": ["fl-owners"]},
        }
    )
    verifier = TokenVerifier(runtime.settings, jwk_client=FakeJwks(rsa_key.public_key()))  # type: ignore[arg-type]
    with TestClient(create_api(runtime, verifier)) as client:
        yield client


def sso_token(key: rsa.RSAPrivateKey, **overrides: Any) -> str:
    now = datetime.now(UTC)
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "00u1abcd",
        "email": "olivia@bank.example",
        "name": "Olivia",
        "iat": now,
        "exp": now + timedelta(hours=1),
        "groups": ["fl-owners", "team-payments", "everyone"],
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256")


def test_sso_maps_groups_to_roles_and_teams(
    sso_client: TestClient, rsa_key: rsa.RSAPrivateKey
) -> None:
    me = sso_client.get("/api/v1/me", headers=auth(sso_token(rsa_key))).json()
    assert me["subject"] == "olivia@bank.example"
    assert me["roles"] == ["app_owner"]
    assert me["teams"] == ["payments"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "api://something-else"},
        {"iss": "https://evil.example.com/"},
        {"exp": datetime.now(UTC) - timedelta(hours=1)},
        {"email": None},
    ],
)
def test_sso_rejects_bad_claims(
    sso_client: TestClient, rsa_key: rsa.RSAPrivateKey, overrides: dict[str, Any]
) -> None:
    response = sso_client.get("/api/v1/me", headers=auth(sso_token(rsa_key, **overrides)))
    assert response.status_code == 401


def test_sso_rejects_a_token_signed_by_another_key(sso_client: TestClient) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert sso_client.get("/api/v1/me", headers=auth(sso_token(other))).status_code == 401


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unsigned(header: dict[str, str], claims: dict[str, Any]) -> str:
    return f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims, default=str).encode())}"


def test_sso_rejects_alg_none_and_algorithm_confusion(
    sso_client: TestClient, rsa_key: rsa.RSAPrivateKey
) -> None:
    now = int(datetime.now(UTC).timestamp())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "x",
        "email": "x@y",
        "iat": now,
        "exp": now + 600,
    }
    none_token = _unsigned({"alg": "none", "typ": "JWT"}, claims) + "."
    assert sso_client.get("/api/v1/me", headers=auth(none_token)).status_code == 401

    # HS256 "signed" with the public key, the classic RS256/HS256 confusion attack.
    public_pem = rsa_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    signing_input = _unsigned({"alg": "HS256", "typ": "JWT"}, claims)
    signature = hmac.new(public_pem, signing_input.encode(), hashlib.sha256).digest()
    confused = f"{signing_input}.{_b64(signature)}"
    assert sso_client.get("/api/v1/me", headers=auth(confused)).status_code == 401


def test_change_is_refused_when_the_audit_log_cannot_be_written(
    make_runtime: Callable[..., Runtime], owner: dict[str, str]
) -> None:
    keyed = make_runtime(audit_key="k" * 40)
    keyed.service.engage_kill_switch(OPERATOR_ACTOR, "Signed with the audit key")
    unkeyed = make_runtime()  # same database, but this process lacks the key
    with TestClient(create_api(unkeyed)) as client:
        response = client.put("/api/v1/apps/payments-api", json=APP_BODY, headers=owner)
        assert response.status_code == 503
        assert response.json()["error"] == "audit_unavailable"
        assert client.get("/api/v1/apps/payments-api", headers=owner).status_code == 404
