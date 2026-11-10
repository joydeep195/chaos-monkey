"""Sign-in: checks bearer tokens and turns them into an Actor with roles and teams.

Production uses single sign-on through the company's identity provider (OpenID
Connect). Tokens must be signed with an asymmetric key published at the provider's
JWKS address, and must carry the expected issuer and audience, an expiry, and a
subject. Roles come from the person's identity-provider groups, mapped in settings.

Development can also use local tokens from `chaosmonkey dev-token`. They are refused
whenever environment = "production".
"""

import secrets
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
from jwt import PyJWKClient

from chaosmonkey.domain import Actor, Role
from chaosmonkey.settings import Settings

DEV_ISSUER = "chaosmonkey-dev"
DEV_AUDIENCE = "chaosmonkey"
DEV_ALGORITHM = "HS256"
DEV_KEY_FILE = Path("data/dev-signing.key")
CLOCK_SKEW_SECONDS = 60
MAX_SUBJECT_LENGTH = 320


class AuthError(Exception):
    """The token was missing, malformed, expired or not trusted. Safe to show."""


def dev_signing_key(settings: Settings) -> bytes:
    """The key for local development tokens: from the environment, else a local file."""
    if settings.is_production:
        raise AuthError("Development tokens are not available in production")
    if settings.dev_signing_key:
        return settings.dev_signing_key.get_secret_value().encode()
    if not DEV_KEY_FILE.is_file():
        DEV_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        DEV_KEY_FILE.write_text(secrets.token_urlsafe(48), encoding="utf-8")
    return DEV_KEY_FILE.read_text(encoding="utf-8").strip().encode()


def issue_dev_token(
    settings: Settings,
    *,
    subject: str,
    roles: Iterable[Role],
    teams: Iterable[str],
    now: datetime,
    hours: float = 8,
) -> str:
    claims = {
        "iss": DEV_ISSUER,
        "aud": DEV_AUDIENCE,
        "sub": subject,
        "name": subject,
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(hours=hours),
        "chaosmonkey_roles": sorted(role.value for role in roles),
        "chaosmonkey_teams": sorted(teams),
    }
    return jwt.encode(claims, dev_signing_key(settings), algorithm=DEV_ALGORITHM)


class TokenVerifier:
    def __init__(self, settings: Settings, jwk_client: PyJWKClient | None = None) -> None:
        self.settings = settings
        api = settings.api
        self.dev_enabled = api.dev_auth and not settings.is_production
        self.oidc_enabled = bool(api.oidc_issuer and api.oidc_audience and api.oidc_jwks_url)
        self._jwks = jwk_client
        if self._jwks is None and self.oidc_enabled and api.oidc_jwks_url:
            self._jwks = PyJWKClient(api.oidc_jwks_url, cache_keys=True, lifespan=300, timeout=5)

    def verify(self, token: str) -> Actor:
        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError as error:
            raise AuthError("Malformed token") from error
        if self.dev_enabled and unverified.get("iss") == DEV_ISSUER:
            return self._verify_dev(token)
        if self.oidc_enabled:
            return self._verify_oidc(token)
        raise AuthError("No sign-in method is configured")

    def _verify_dev(self, token: str) -> Actor:
        try:
            claims = jwt.decode(
                token,
                dev_signing_key(self.settings),
                algorithms=[DEV_ALGORITHM],
                audience=DEV_AUDIENCE,
                issuer=DEV_ISSUER,
                leeway=CLOCK_SKEW_SECONDS,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
        except jwt.PyJWTError as error:
            raise AuthError(_describe(error)) from error
        roles = {
            Role(value) for value in _strings(claims.get("chaosmonkey_roles")) if value in Role
        }
        teams = set(_strings(claims.get("chaosmonkey_teams")))
        return _actor(claims, "sub", roles, teams)

    def _verify_oidc(self, token: str) -> Actor:
        api = self.settings.api
        assert self._jwks is not None  # noqa: S101 - set whenever OIDC is enabled
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=list(api.allowed_algorithms),
                audience=api.oidc_audience,
                issuer=api.oidc_issuer,
                leeway=CLOCK_SKEW_SECONDS,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
        except jwt.PyJWTError as error:
            raise AuthError(_describe(error)) from error
        groups = set(_strings(claims.get(api.groups_claim)))
        roles = {role for role, granted_by in api.role_groups.items() if groups & set(granted_by)}
        prefix = api.team_group_prefix
        teams = {g.removeprefix(prefix) for g in groups if prefix and g.startswith(prefix)}
        return _actor(claims, api.subject_claim, roles, teams)


def _actor(claims: dict[str, Any], subject_claim: str, roles: set[Role], teams: set[str]) -> Actor:
    subject = claims.get(subject_claim)
    if not isinstance(subject, str) or not subject or len(subject) > MAX_SUBJECT_LENGTH:
        raise AuthError(f"Token has no usable {subject_claim} claim")
    name = claims.get("name") or claims.get("preferred_username") or subject
    return Actor(
        subject=subject,
        display_name=str(name)[:200],
        roles=frozenset(roles),
        teams=frozenset(teams),
        channel="api",
    )


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _describe(error: jwt.PyJWTError) -> str:
    if isinstance(error, jwt.ExpiredSignatureError):
        return "Token has expired"
    if isinstance(error, jwt.InvalidAudienceError):
        return "Token is for a different audience"
    if isinstance(error, jwt.InvalidIssuerError):
        return "Token is from an untrusted issuer"
    if isinstance(error, jwt.MissingRequiredClaimError):
        return f"Token is missing the {error.claim} claim"
    return "Token could not be verified"
