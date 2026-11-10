"""Database tables and connection handling.

PostgreSQL in production, SQLite on a single machine. Every time is stored in UTC.
Constraints in the schema, not just code, stop two copies of Chaos Monkey from
planning or carrying out the same kill twice.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Dialect,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


class UTCDateTime(TypeDecorator[datetime]):
    """Stores timezone-aware datetimes as UTC and always hands back aware UTC values."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Refusing to store a datetime without a time zone")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    pass


class AppRow(Base):
    __tablename__ = "apps"

    name: Mapped[str] = mapped_column(String(63), primary_key=True)
    owner_team: Mapped[str] = mapped_column(String(63), index=True)
    enabled: Mapped[bool]
    mode: Mapped[str] = mapped_column(String(10))
    mean_days_between_kills: Mapped[float]
    min_days_between_kills: Mapped[int]
    grouping: Mapped[str] = mapped_column(String(10))
    regions_independent: Mapped[bool]
    min_healthy: Mapped[int]
    exceptions: Mapped[list[dict[str, str]]] = mapped_column(JSON)
    # Goes up by one on every change. An approval covers exactly one version.
    config_version: Mapped[int]
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_by: Mapped[str] = mapped_column(String(320))


class ApprovalRow(Base):
    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    app_name: Mapped[str] = mapped_column(ForeignKey("apps.name"), index=True)
    config_version: Mapped[int]
    # pending, approved, rejected or superseded
    status: Mapped[str] = mapped_column(String(12), index=True)
    reason: Mapped[str] = mapped_column(Text)
    requested_by: Mapped[str] = mapped_column(String(320))
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime)
    decided_by: Mapped[str | None] = mapped_column(String(320))
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    decision_note: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class ScheduleDayRow(Base):
    """One row per local day that has been planned, so a day is never planned twice."""

    __tablename__ = "schedule_days"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    planned: Mapped[int]


class ScheduledKillRow(Base):
    __tablename__ = "scheduled_kills"
    __table_args__ = (UniqueConstraint("day", "group_key", name="one_kill_per_group_per_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    app_name: Mapped[str] = mapped_column(String(63), index=True)
    group_key: Mapped[str] = mapped_column(String(400))
    run_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    # pending, running, done, skipped or failed
    status: Mapped[str] = mapped_column(String(10), index=True)
    outcome: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class TerminationRow(Base):
    __tablename__ = "terminations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scheduled_kill_id: Mapped[int] = mapped_column(ForeignKey("scheduled_kills.id"), unique=True)
    app_name: Mapped[str] = mapped_column(String(63), index=True)
    group_key: Mapped[str] = mapped_column(String(400), index=True)
    target_id: Mapped[str] = mapped_column(String(255))
    mode: Mapped[str] = mapped_column(String(10))
    healthy_before: Mapped[int]
    executed_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    # not_applicable (dry run), watching, recovered or failed
    recovery_status: Mapped[str] = mapped_column(String(16), index=True)
    recovery_deadline: Mapped[datetime | None] = mapped_column(UTCDateTime)
    recovered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class ControlRow(Base):
    """Small pieces of shared state: the kill switch, a declared incident, the audit head."""

    __tablename__ = "controls"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class LeaseRow(Base):
    """Leader election: only the holder of the executor lease carries out kills."""

    __tablename__ = "leases"

    name: Mapped[str] = mapped_column(String(50), primary_key=True)
    holder: Mapped[str] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)


class FreezeWindowRow(Base):
    __tablename__ = "freeze_windows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200))
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    ends_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    created_by: Mapped[str] = mapped_column(String(320))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AuditRow(Base):
    """Append-only, hash-chained record of everything that happens. Never updated."""

    __tablename__ = "audit_log"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    actor: Mapped[str] = mapped_column(String(320))
    action: Mapped[str] = mapped_column(String(64), index=True)
    subject: Mapped[str] = mapped_column(String(400))
    details: Mapped[dict[str, Any]] = mapped_column(JSON)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class Database:
    def __init__(self, url: str) -> None:
        parsed = make_url(url)
        self.is_sqlite = parsed.get_backend_name() == "sqlite"
        connect_args: dict[str, Any] = {}
        if self.is_sqlite:
            if parsed.database and parsed.database != ":memory:":
                Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
            connect_args["check_same_thread"] = False
        self.engine: Engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
        if self.is_sqlite:
            _configure_sqlite(self.engine)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        """One unit of work: commits on success, rolls back on any error."""
        with self._sessions.begin() as session:
            yield session

    def dispose(self) -> None:
        self.engine.dispose()


def _configure_sqlite(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        # Let SQLAlchemy issue BEGIN itself, so every transaction takes the write lock up
        # front (BEGIN IMMEDIATE) and two writers queue instead of interleaving.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA busy_timeout = 10000")
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(connection: Any) -> None:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
