"""Core vocabulary: apps, targets, groups, roles and the people acting on them.

Naming follows Netflix's Chaos Monkey: an app is deployed as clusters named
app-stack-detail, in accounts and regions. A "target" is one instance (a pod or a
server) that Chaos Monkey may terminate.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

WILDCARD = "*"

AppName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,62}$")]
TeamName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,62}$")]
# Accounts, regions, stacks and details: letters, digits, dot, dash, underscore, or "*".
Selector = Annotated[str, StringConstraints(pattern=r"^(\*|[A-Za-z0-9._-]{0,63})$")]


class Role(StrEnum):
    VIEWER = "viewer"
    APP_OWNER = "app_owner"
    APPROVER = "approver"
    OPERATOR = "operator"
    ADMIN = "admin"


class Grouping(StrEnum):
    """How an app's instances are grouped. Each group loses at most one instance a day."""

    APP = "app"
    STACK = "stack"
    CLUSTER = "cluster"


class Mode(StrEnum):
    DRY_RUN = "dry_run"
    LIVE = "live"


@dataclass(frozen=True)
class Target:
    """One instance Chaos Monkey could terminate, as reported by a driver."""

    id: str
    app: str
    account: str
    region: str
    stack: str = ""
    detail: str = ""
    healthy: bool = True
    # Carries the opt-out label or tag (for example a ledger's primary database).
    protected: bool = False

    @property
    def cluster(self) -> str:
        """Cluster name in Netflix's app-stack-detail convention."""
        if self.detail:
            return f"{self.app}-{self.stack}-{self.detail}"
        if self.stack:
            return f"{self.app}-{self.stack}"
        return self.app


@dataclass(frozen=True, order=True)
class GroupKey:
    """A group of targets that shares one schedule. "*" in a field means any value."""

    app: str
    account: str
    region: str
    stack: str
    cluster: str

    @classmethod
    def for_target(cls, target: Target, grouping: Grouping, regions_independent: bool) -> Self:
        region = target.region if regions_independent else WILDCARD
        stack = WILDCARD if grouping is Grouping.APP else target.stack
        cluster = target.cluster if grouping is Grouping.CLUSTER else WILDCARD
        return cls(target.app, target.account, region, stack, cluster)

    def matches(self, target: Target) -> bool:
        return (
            target.app == self.app
            and target.account == self.account
            and self.region in (WILDCARD, target.region)
            and self.stack in (WILDCARD, target.stack)
            and self.cluster in (WILDCARD, target.cluster)
        )

    def __str__(self) -> str:
        return "/".join((self.app, self.account, self.region, self.stack or "-", self.cluster))

    @classmethod
    def parse(cls, text: str) -> Self:
        app, account, region, stack, cluster = text.split("/")
        return cls(app, account, region, "" if stack == "-" else stack, cluster)


class ExceptionRule(BaseModel):
    """Opts a slice of an app out, as in Netflix's original. "*" matches anything."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    account: Selector = WILDCARD
    region: Selector = WILDCARD
    stack: Selector = WILDCARD
    detail: Selector = WILDCARD

    def matches(self, target: Target) -> bool:
        return (
            self.account in (WILDCARD, target.account)
            and self.region in (WILDCARD, target.region)
            and self.stack in (WILDCARD, target.stack)
            and self.detail in (WILDCARD, target.detail)
        )


class AppConfig(BaseModel):
    """What an app owner controls. Defaults match Netflix's original, plus a capacity floor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: AppName
    owner_team: TeamName
    enabled: bool = False
    mode: Mode = Mode.DRY_RUN
    mean_days_between_kills: float = Field(default=2.0, ge=1, le=365)
    min_days_between_kills: int = Field(default=1, ge=1, le=60)
    grouping: Grouping = Grouping.CLUSTER
    regions_independent: bool = True
    min_healthy: int = Field(default=2, ge=1, le=10_000)
    exceptions: tuple[ExceptionRule, ...] = Field(default=(), max_length=50)

    def is_excepted(self, target: Target) -> bool:
        return any(rule.matches(target) for rule in self.exceptions)


def eligible_targets(app: AppConfig, targets: list[Target]) -> list[Target]:
    """Targets of this app that may ever be touched: not protected and not opted out."""
    return [t for t in targets if t.app == app.name and not t.protected and not app.is_excepted(t)]


@dataclass(frozen=True)
class Actor:
    """A person or system acting on Chaos Monkey. Every audit entry names one."""

    subject: str
    display_name: str
    roles: frozenset[Role]
    teams: frozenset[str] = frozenset()
    channel: str = "api"  # api, cli or system

    @property
    def label(self) -> str:
        return f"{self.channel}:{self.subject}"


SYSTEM_ACTOR = Actor(
    subject="chaosmonkey", display_name="Chaos Monkey", roles=frozenset(), channel="system"
)
