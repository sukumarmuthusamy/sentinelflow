"""Durable job metadata, idempotency keys and lifecycle events in SQLite (ADR-0003).

Concurrency model
-----------------
Every write runs in its own connection inside ``BEGIN IMMEDIATE``, which takes
SQLite's write lock before reading. Each of these is therefore one atomic step, across
threads *and* processes that share the file:

* the idempotency reservation (look up the key, enforce the active-job cap, insert the job
  and its ``job.queued`` event);
* every status transition, a compare-and-set on the current status that writes the
  matching lifecycle events in the same transaction.

What this does **not** give is ownership of execution. A job runs as an asyncio task
in the process that accepted it, and the store has no lease or heartbeat. Startup
recovery (``JobService.recover``) assumes one API process per database file.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.errors import ErrorCode, SentinelFlowError
from app.jobs.events import JobEvent, JobEventType
from app.jobs.models import (
    ACTIVE_STATUSES,
    ApprovalRecord,
    JobError,
    JobKind,
    JobRecord,
    JobStatus,
    is_allowed_transition,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    request_json TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    idempotency_scope TEXT,
    idempotency_key_hash TEXT,
    thread_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    run_count INTEGER NOT NULL DEFAULT 0,
    approval_json TEXT,
    result_json TEXT,
    error_json TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_idempotency
    ON jobs (idempotency_scope, idempotency_key_hash)
    WHERE idempotency_key_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status);
CREATE TABLE IF NOT EXISTS job_events (
    job_id TEXT NOT NULL REFERENCES jobs (job_id),
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    at TEXT NOT NULL,
    data_json TEXT NOT NULL,
    PRIMARY KEY (job_id, seq)
);
"""

_UNSET: Any = object()
# f-string SQL below interpolates only fixed column names and "?" placeholders;
# every value is bound as a parameter.

EventSpec = tuple[JobEventType, dict[str, Any]]


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True, separators=(",", ":"))


def _model_json(value: ApprovalRecord | JobError | None) -> str | None:
    return _json(value.model_dump(mode="json")) if value is not None else None


@dataclass(frozen=True)
class CreateOutcome:
    """Result of an idempotent create: the job, and whether this call inserted it."""

    record: JobRecord
    created: bool


class SqliteJobStore:
    """Job store on one SQLite file; safe to share between threads and processes."""

    def __init__(self, path: Path, *, busy_timeout_seconds: float = 10.0) -> None:
        self._path = path
        self._timeout = busy_timeout_seconds

    @property
    def path(self) -> Path:
        return self._path

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=self._timeout, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    # -- reads -----------------------------------------------------------------

    def get(self, job_id: str) -> JobRecord | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return _record(row) if row is not None else None

    def find_by_idempotency(self, scope: str, key_hash: str) -> JobRecord | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE idempotency_scope = ? AND idempotency_key_hash = ?",
                (scope, key_hash),
            ).fetchone()
        return _record(row) if row is not None else None

    def list_by_status(self, statuses: Sequence[JobStatus]) -> list[JobRecord]:
        marks = ",".join("?" for _ in statuses)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY created_at",
                [status.value for status in statuses],
            ).fetchall()
        return [_record(row) for row in rows]

    def events(self, job_id: str, *, after_seq: int = 0) -> list[JobEvent]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM job_events WHERE job_id = ? AND seq > ? ORDER BY seq",
                (job_id, after_seq),
            ).fetchall()
        return [
            JobEvent(
                job_id=row["job_id"],
                seq=row["seq"],
                type=JobEventType(row["type"]),
                at=datetime.fromisoformat(row["at"]),
                data=json.loads(row["data_json"]),
            )
            for row in rows
        ]

    # -- writes ----------------------------------------------------------------

    def create(
        self,
        record: JobRecord,
        *,
        idempotency_scope: str | None = None,
        idempotency_key_hash: str | None = None,
        max_active: int,
    ) -> CreateOutcome:
        """Insert a queued job, or return the job that already owns the key.

        The key lookup, the active-job cap and the insert happen under one write
        lock, so of N concurrent creates with one key exactly one inserts.
        """
        if record.status is not JobStatus.QUEUED:
            msg = "new jobs start queued"
            raise ValueError(msg)
        with self._write() as conn:
            if idempotency_key_hash is not None:
                existing = conn.execute(
                    "SELECT * FROM jobs WHERE idempotency_scope = ? AND idempotency_key_hash = ?",
                    (idempotency_scope, idempotency_key_hash),
                ).fetchone()
                if existing is not None:
                    return CreateOutcome(record=_record(existing), created=False)
            marks = ",".join("?" for _ in ACTIVE_STATUSES)
            (active,) = conn.execute(
                f"SELECT COUNT(*) FROM jobs WHERE status IN ({marks})",
                [status.value for status in ACTIVE_STATUSES],
            ).fetchone()
            if active >= max_active:
                raise SentinelFlowError(
                    "Too many queued or running jobs; retry later",
                    code=ErrorCode.TOO_MANY_ACTIVE_JOBS,
                    details={"max_active_jobs": max_active},
                )
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, kind, status, request_json, request_fingerprint,
                    idempotency_scope, idempotency_key_hash, thread_id,
                    created_at, updated_at, run_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    record.job_id,
                    record.kind.value,
                    record.status.value,
                    _json(record.request),
                    record.request_fingerprint,
                    idempotency_scope if idempotency_key_hash is not None else None,
                    idempotency_key_hash,
                    record.thread_id,
                    _iso(record.created_at),
                    _iso(record.updated_at),
                ),
            )
            _append_events(conn, record.job_id, [(JobEventType.JOB_QUEUED, {})], record.created_at)
        return CreateOutcome(record=record, created=True)

    def transition(
        self,
        job_id: str,
        *,
        expected: JobStatus,
        target: JobStatus,
        events: Sequence[EventSpec],
        started_at: datetime | None = _UNSET,
        finished_at: datetime | None = _UNSET,
        increment_run_count: bool = False,
        approval: ApprovalRecord | None = _UNSET,
        result: dict[str, Any] | None = _UNSET,
        error: JobError | None = _UNSET,
    ) -> JobRecord | None:
        """Compare-and-set ``expected -> target``; ``None`` when the job is not in ``expected``.

        Raises ``ValueError`` for a move the lifecycle table forbids, so a coding
        error cannot be mistaken for a lost race.
        """
        if not is_allowed_transition(expected, target):
            msg = f"illegal job transition {expected.value} -> {target.value}"
            raise ValueError(msg)
        now = utc_now()
        sets = ["status = ?", "updated_at = ?"]
        params: list[Any] = [target.value, _iso(now)]
        updates: list[tuple[str, str | None]] = []
        if started_at is not _UNSET:
            updates.append(("started_at", _iso(started_at)))
        if finished_at is not _UNSET:
            updates.append(("finished_at", _iso(finished_at)))
        if approval is not _UNSET:
            updates.append(("approval_json", _model_json(approval)))
        if result is not _UNSET:
            updates.append(("result_json", _json(result)))
        if error is not _UNSET:
            updates.append(("error_json", _model_json(error)))
        for column, encoded in updates:
            sets.append(f"{column} = ?")
            params.append(encoded)
        if increment_run_count:
            sets.append("run_count = run_count + 1")
        with self._write() as conn:
            cursor = conn.execute(
                f"UPDATE jobs SET {', '.join(sets)} WHERE job_id = ? AND status = ?",
                [*params, job_id, expected.value],
            )
            if cursor.rowcount != 1:
                return None
            _append_events(conn, job_id, events, now)
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return _record(row)


def _append_events(
    conn: sqlite3.Connection,
    job_id: str,
    events: Sequence[EventSpec],
    at: datetime,
) -> None:
    (last,) = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) FROM job_events WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    for offset, (event_type, data) in enumerate(events, start=1):
        conn.execute(
            "INSERT INTO job_events (job_id, seq, type, at, data_json) VALUES (?, ?, ?, ?, ?)",
            (job_id, last + offset, event_type.value, _iso(at), _json(data) or "{}"),
        )


def _record(row: sqlite3.Row) -> JobRecord:
    approval = json.loads(row["approval_json"]) if row["approval_json"] else None
    error = json.loads(row["error_json"]) if row["error_json"] else None
    created = _dt(row["created_at"])
    updated = _dt(row["updated_at"])
    assert created is not None and updated is not None
    return JobRecord(
        job_id=row["job_id"],
        kind=JobKind(row["kind"]),
        status=JobStatus(row["status"]),
        request=json.loads(row["request_json"]),
        request_fingerprint=row["request_fingerprint"],
        thread_id=row["thread_id"],
        created_at=created,
        updated_at=updated,
        started_at=_dt(row["started_at"]),
        finished_at=_dt(row["finished_at"]),
        run_count=row["run_count"],
        approval=ApprovalRecord.model_validate(approval) if approval else None,
        result=json.loads(row["result_json"]) if row["result_json"] else None,
        error=JobError.model_validate(error) if error else None,
    )
