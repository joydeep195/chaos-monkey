"""Tamper-evident audit log.

Each entry stores the hash of the entry before it. Changing or deleting any entry
breaks the chain from that point on, and `verify` reports where. With an audit key
(CHAOSMONKEY_AUDIT_KEY, required in production) the hashes are HMACs, so someone who
can write to the database still cannot forge a valid chain without the key.

Removing the newest entries cannot be detected from inside the database alone.
Export the log regularly to write-once storage and check the head you recorded with
`chaosmonkey audit verify --expect-head SEQ:HASH`.
"""

import hashlib
import hmac
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from chaosmonkey.db import AuditRow, ControlRow

GENESIS_HASH = "0" * 64
HEAD_KEY = "audit_head"


class AuditError(Exception):
    pass


@dataclass(frozen=True)
class AuditEntry:
    seq: int
    at: datetime
    actor: str
    action: str
    subject: str
    details: dict[str, Any]
    prev_hash: str
    hash: str

    @classmethod
    def from_row(cls, row: AuditRow) -> AuditEntry:
        return cls(
            row.seq,
            row.at,
            row.actor,
            row.action,
            row.subject,
            row.details,
            row.prev_hash,
            row.hash,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at": self.at.isoformat(),
            "actor": self.actor,
            "action": self.action,
            "subject": self.subject,
            "details": self.details,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }


@dataclass(frozen=True)
class VerifyReport:
    ok: bool
    entries: int
    head_seq: int
    head_hash: str
    problem: str | None = None
    first_bad_seq: int | None = None


def _algorithm(key: bytes | None) -> str:
    return "hmac-sha256" if key else "sha256"


def compute_hash(
    *,
    seq: int,
    at: datetime,
    actor: str,
    action: str,
    subject: str,
    details: dict[str, Any],
    prev_hash: str,
    key: bytes | None,
) -> str:
    payload = json.dumps(
        {
            "seq": seq,
            "at": at.isoformat(),
            "actor": actor,
            "action": action,
            "subject": subject,
            "details": details,
            "prev_hash": prev_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    if key:
        return hmac.new(key, payload, hashlib.sha256).hexdigest()
    return hashlib.sha256(payload).hexdigest()


def append(
    session: Session,
    *,
    at: datetime,
    actor: str,
    action: str,
    subject: str,
    details: dict[str, Any] | None = None,
    key: bytes | None,
) -> AuditEntry:
    """Add an entry in the caller's transaction, so the change and its record commit together."""
    details = details or {}
    # Locking the head row serialises writers on PostgreSQL; SQLite already takes the
    # write lock at the start of every transaction (see db.py).
    head = session.get(ControlRow, HEAD_KEY, with_for_update=True)
    if head is None:
        seq, prev_hash, algorithm = 1, GENESIS_HASH, _algorithm(key)
    else:
        seq, prev_hash, algorithm = head.value["seq"] + 1, head.value["hash"], head.value["alg"]
        if algorithm != _algorithm(key):
            raise AuditError(
                f"The audit log is signed with {algorithm} but this process has "
                f"{'an' if key else 'no'} audit key. Set CHAOSMONKEY_AUDIT_KEY to the same "
                "value as the rest of the deployment."
            )
    # Store and hash the same value: the database keeps UTC.
    at = at.astimezone(UTC)
    entry_hash = compute_hash(
        seq=seq,
        at=at,
        actor=actor,
        action=action,
        subject=subject,
        details=details,
        prev_hash=prev_hash,
        key=key,
    )
    session.add(
        AuditRow(
            seq=seq,
            at=at,
            actor=actor,
            action=action,
            subject=subject,
            details=details,
            prev_hash=prev_hash,
            hash=entry_hash,
        )
    )
    value = {"seq": seq, "hash": entry_hash, "alg": algorithm}
    if head is None:
        session.add(ControlRow(key=HEAD_KEY, value=value, updated_at=at))
    else:
        head.value = value
        head.updated_at = at
    session.flush()
    return AuditEntry(seq, at, actor, action, subject, details, prev_hash, entry_hash)


def entries(session: Session, *, after_seq: int = 0, limit: int = 100) -> list[AuditEntry]:
    rows = session.scalars(
        select(AuditRow).where(AuditRow.seq > after_seq).order_by(AuditRow.seq).limit(limit)
    )
    return [AuditEntry.from_row(row) for row in rows]


def iterate(session: Session, batch: int = 1_000) -> Iterator[AuditEntry]:
    after = 0
    while True:
        page = entries(session, after_seq=after, limit=batch)
        if not page:
            return
        yield from page
        after = page[-1].seq


def verify(
    session: Session, *, key: bytes | None, expect_head: tuple[int, str] | None = None
) -> VerifyReport:
    """Recompute the whole chain and compare it with what is stored."""
    head = session.get(ControlRow, HEAD_KEY)
    expected_seq, prev_hash, count = 1, GENESIS_HASH, 0
    if head is not None and head.value["alg"] != _algorithm(key):
        return VerifyReport(
            False, 0, 0, GENESIS_HASH, f"Log is signed with {head.value['alg']}; wrong audit key"
        )
    for entry in iterate(session):
        if entry.seq != expected_seq:
            return VerifyReport(
                False,
                count,
                expected_seq - 1,
                prev_hash,
                f"Entry {expected_seq} is missing (next entry found is {entry.seq})",
                expected_seq,
            )
        if entry.prev_hash != prev_hash:
            return VerifyReport(
                False,
                count,
                entry.seq - 1,
                prev_hash,
                f"Entry {entry.seq} does not link to the entry before it",
                entry.seq,
            )
        recomputed = compute_hash(
            seq=entry.seq,
            at=entry.at,
            actor=entry.actor,
            action=entry.action,
            subject=entry.subject,
            details=entry.details,
            prev_hash=entry.prev_hash,
            key=key,
        )
        if not hmac.compare_digest(recomputed, entry.hash):
            return VerifyReport(
                False,
                count,
                entry.seq - 1,
                prev_hash,
                f"Entry {entry.seq} has been altered (its hash does not match its contents)",
                entry.seq,
            )
        prev_hash, expected_seq, count = entry.hash, entry.seq + 1, count + 1

    last_seq = expected_seq - 1
    if head is not None and (head.value["seq"], head.value["hash"]) != (last_seq, prev_hash):
        return VerifyReport(
            False,
            count,
            last_seq,
            prev_hash,
            f"The newest entries are missing: the log should end at entry {head.value['seq']}",
            last_seq + 1,
        )
    if expect_head is not None:
        want_seq, want_hash = expect_head
        if want_seq > last_seq:
            return VerifyReport(
                False,
                count,
                last_seq,
                prev_hash,
                f"The log ends at entry {last_seq}, before the recorded head {want_seq}",
                last_seq + 1,
            )
        anchor = session.get(AuditRow, want_seq)
        if anchor is None or not hmac.compare_digest(anchor.hash, want_hash):
            return VerifyReport(
                False,
                count,
                last_seq,
                prev_hash,
                f"Entry {want_seq} does not match the recorded head",
                want_seq,
            )
    return VerifyReport(True, count, last_seq, prev_hash)
