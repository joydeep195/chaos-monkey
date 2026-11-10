"""The audit log detects edits, deletions and a wrong key."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, update

from chaosmonkey import audit
from chaosmonkey.db import AuditRow, Database

KEY = b"k" * 32
START = datetime(2026, 11, 9, 9, tzinfo=UTC)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{(tmp_path / 'audit.db').as_posix()}")
    database.create_schema()
    return database


def write(db: Database, count: int, key: bytes | None = KEY) -> None:
    for n in range(count):
        with db.transaction() as session:
            audit.append(
                session,
                at=START + timedelta(minutes=n),
                actor="api:alice",
                action="test.event",
                subject=f"thing-{n}",
                details={"n": n, "note": "café"},
                key=key,
            )


def check(db: Database, key: bytes | None = KEY, **kwargs: object) -> audit.VerifyReport:
    with db.transaction() as session:
        return audit.verify(session, key=key, **kwargs)  # type: ignore[arg-type]


def test_intact_chain_verifies(db: Database) -> None:
    write(db, 5)
    report = check(db)
    assert report.ok
    assert report.entries == 5
    assert report.head_seq == 5


def test_unkeyed_chain_verifies(db: Database) -> None:
    write(db, 3, key=None)
    assert check(db, key=None).ok


def test_edited_entry_is_detected(db: Database) -> None:
    write(db, 5)
    with db.transaction() as session:
        session.execute(update(AuditRow).where(AuditRow.seq == 3).values(actor="api:mallory"))
    report = check(db)
    assert not report.ok
    assert report.first_bad_seq == 3
    assert "altered" in (report.problem or "")


def test_deleted_middle_entry_is_detected(db: Database) -> None:
    write(db, 5)
    with db.transaction() as session:
        session.execute(delete(AuditRow).where(AuditRow.seq == 2))
    report = check(db)
    assert not report.ok
    assert report.first_bad_seq == 2


def test_deleted_newest_entry_is_detected_by_the_head(db: Database) -> None:
    write(db, 5)
    with db.transaction() as session:
        session.execute(delete(AuditRow).where(AuditRow.seq == 5))
    report = check(db)
    assert not report.ok
    assert "newest" in (report.problem or "")


def test_recomputed_chain_without_the_key_is_detected(db: Database) -> None:
    write(db, 3)
    # An attacker rewrites entry 2 and recomputes its hash without knowing the key.
    with db.transaction() as session:
        row = session.get_one(AuditRow, 2)
        forged = audit.compute_hash(
            seq=2,
            at=row.at,
            actor="api:mallory",
            action=row.action,
            subject=row.subject,
            details=row.details,
            prev_hash=row.prev_hash,
            key=b"wrong-key-wrong-key-wrong-key-xx",
        )
        row.actor, row.hash = "api:mallory", forged
    assert not check(db).ok


def test_wrong_key_fails_verification(db: Database) -> None:
    write(db, 2)
    assert not check(db, key=b"x" * 32).ok


def test_mixing_keyed_and_unkeyed_writers_is_refused(db: Database) -> None:
    write(db, 1, key=KEY)
    with pytest.raises(audit.AuditError):
        write(db, 1, key=None)


def test_expected_head_catches_truncation_after_the_fact(db: Database) -> None:
    write(db, 4)
    head = check(db)
    recorded = (head.head_seq, head.head_hash)
    assert check(db, expect_head=recorded).ok
    # Someone deletes the newest entry and rewinds the stored head to match.
    with db.transaction() as session:
        session.execute(delete(AuditRow).where(AuditRow.seq == 4))
        previous = session.get_one(AuditRow, 3)
        head_row = session.get_one(audit.ControlRow, audit.HEAD_KEY)
        head_row.value = {"seq": 3, "hash": previous.hash, "alg": "hmac-sha256"}
    assert check(db).ok  # undetectable from inside the database...
    assert not check(db, expect_head=recorded).ok  # ...but caught by the recorded head
