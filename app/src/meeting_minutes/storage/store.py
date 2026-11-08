"""Transcript repository: the only code that runs SQL.

Every change is one explicit `BEGIN IMMEDIATE` transaction; a failure rolls the whole
change back and raises `StorageError` with a fixed, content-free code. One connection
is shared under a lock, so writes are serialized (the meeting session's writer thread
and the UI both go through here).

Rules kept here because SQL alone does not enforce them:
- a segment's `current_revision` always names an existing revision;
- original ASR text (revision 1, origin 'asr') is kept until redaction or deletion;
  corrections append a revision with origin 'user';
- only the current text of each segment and the meeting title are indexed, updated in
  the same transaction as the text;
- every transcript change increments `meetings.transcript_revision`;
- redaction and deletion remove the text, every revision and the index entries, then
  merge the FTS index and truncate the WAL so the text does not linger in the files;
- retention cleanup (`purge_expired`) deletes only finished meetings past the
  cutoff, a bounded batch per transaction, all or nothing;
- optional OpenAI minutes are derived data: deleting a meeting deletes them, and any
  redaction in a meeting invalidates all its minutes versions and deletes their
  generated/edited text and actions in the same transaction. A result
  generated from a snapshot that is no longer valid is refused when saved.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from meeting_minutes.storage.schema import connect, migrate
from meeting_minutes.storage.search import MAX_PAGE, literal_query, normalize

OWNER_ID = "00000000-0000-4000-8000-000000000001"  # the single local owner
AGENT_ID = "00000000-0000-4000-8000-000000000002"  # this computer's capture agent
# Meetings that were still running when the app ended are recovered as interrupted.
UNFINISHED = ("starting", "capturing", "paused", "degraded", "stopping")
MAINTENANCE_KEY = "maintenance_pending"  # settings row: a tidy-up after deletion is owed


class StorageError(RuntimeError):
    """A failed store operation. `code` is fixed text with no content or paths:
    disk_full, read_only, busy, io_error, corrupt or failed."""

    def __init__(self, code: str) -> None:
        super().__init__(f"transcript storage error: {code}")
        self.code = code


class MaintenanceError(StorageError):
    """A deletion or redaction was committed, but the maintenance that follows it
    (merging the search index, folding the WAL back) failed. The removed rows are
    gone and no longer readable through the app; `deleted` lists the meetings that
    were deleted (empty for a redaction). Retry with `scrub()`."""

    def __init__(self, code: str, deleted: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.deleted = deleted


_ERROR_CODES = {
    "SQLITE_FULL": "disk_full",
    "SQLITE_READONLY": "read_only",
    "SQLITE_BUSY": "busy",
    "SQLITE_LOCKED": "busy",
    "SQLITE_IOERR": "io_error",
    "SQLITE_CORRUPT": "corrupt",
    "SQLITE_NOTADB": "corrupt",
}


def _code(exc: sqlite3.Error) -> str:
    name = getattr(exc, "sqlite_errorname", "") or ""
    return next((code for prefix, code in _ERROR_CODES.items() if name.startswith(prefix)), "failed")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


_ZONE_OFFSET = re.compile(r"\(UTC([+-]\d{2}:\d{2})\)$")
_UTC_NAMES = {"UTC", "GMT", "Etc/UTC", "Coordinated Universal Time"}


def local_zone_label(now: datetime | None = None) -> str:
    """The meeting's `timezone` value for a new meeting: this PC's zone name and its
    UTC offset at the start, e.g. "Egypt Standard Time (UTC+02:00)"."""
    local = now if now is not None and now.tzinfo is not None else (now or datetime.now()).astimezone()
    offset = local.strftime("%z")
    return f"{local.tzname() or 'local'} (UTC{offset[:3]}:{offset[3:5]})"


def zone_offset(label: str) -> str | None:
    """The original UTC offset ("+02:00") recorded in a meeting's `timezone` value, or
    None when it can't be known reliably (older meetings stored only a zone name)."""
    match = _ZONE_OFFSET.search(label or "")
    if match:
        return match.group(1)
    return "+00:00" if (label or "").strip() in _UTC_NAMES else None


@dataclass(frozen=True, slots=True)
class MeetingRow:
    id: str
    title: str
    started_at: str
    ended_at: str | None
    timezone: str
    status: str
    transcript_revision: int
    segments: int = 0


@dataclass(frozen=True, slots=True)
class SegmentView:
    """A segment with its current revision."""

    id: str
    source: str
    epoch: int
    start_ms: int
    end_ms: int
    revision: int
    text: str = field(repr=False)
    language: str
    origin: str
    flags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RevisionView:
    revision: int
    text: str = field(repr=False)
    language: str
    origin: str
    edited_at: str


@dataclass(frozen=True, slots=True)
class TimelineRow:
    id: str
    kind: str
    source: str | None
    start_ms: int
    end_ms: int | None
    code: str
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MeetingSnapshot:
    """One meeting read in a single hold of the store: no write can land between
    its parts, so it is one consistent saved revision (used for display and export)."""

    meeting: MeetingRow
    segments: tuple[SegmentView, ...]
    timeline: tuple[TimelineRow, ...]


class MinutesRejected(RuntimeError):
    """A minutes change the store refused. `code` is fixed text with no content:
    meeting_deleted, transcript_redacted, evidence_missing, invalidated, read_only,
    not_found or bad_state."""

    def __init__(self, code: str) -> None:
        super().__init__(f"minutes change refused: {code}")
        self.code = code


@dataclass(frozen=True, slots=True)
class MinutesVersionRow:
    """One saved minutes version. Invalidated versions keep only this
    content-free lineage; their generated text and actions are deleted."""

    id: str
    meeting_id: str
    version: int
    parent_version: int | None
    transcript_revision: int
    snapshot_hash: str
    provider: str
    model_id: str
    prompt_version: str
    schema_version: int
    output_language: str
    input_tokens: int | None
    output_tokens: int | None
    requests: int
    parts: int
    incomplete: bool
    warnings: tuple[str, ...] = field(repr=False)
    state: str
    user_edited: bool
    created_at: str
    updated_at: str
    invalidated: bool
    invalidated_reason: str | None


@dataclass(frozen=True, slots=True)
class MinutesItemRow:
    id: str
    version_id: str
    kind: str  # summary | topic | decision | question | action
    position: int
    text: str = field(repr=False)
    detail: str | None = field(repr=False)
    status: str | None
    owner: str | None = field(repr=False)
    due_date: str | None
    due_date_text: str | None = field(repr=False)
    date_status: str | None
    commitment: str | None
    origin: str  # model | user
    reviewed: bool
    evidence: tuple[dict[str, Any], ...]


MINUTES_ITEM_FIELDS = ("text", "detail", "status", "owner", "due_date", "due_date_text", "date_status",
                       "commitment", "reviewed")
_VERSION_COLUMNS = ("id, meeting_id, version, parent_version, transcript_revision, snapshot_hash, provider, model_id,"
                    " prompt_version, schema_version, output_language, input_tokens, output_tokens, requests, parts,"
                    " incomplete, warnings_json, state, user_edited, created_at, updated_at, invalidated,"
                    " invalidated_reason")
_ITEM_COLUMNS = ("id, version_id, kind, position, text, detail, status, owner, due_date, due_date_text, date_status,"
                 " commitment, origin, reviewed, evidence_json")


def _version_row(r: tuple[Any, ...]) -> MinutesVersionRow:
    return MinutesVersionRow(*r[:15], bool(r[15]), tuple(json.loads(r[16])), r[17], bool(r[18]), r[19], r[20],
                             bool(r[21]), r[22])


def _item_row(r: tuple[Any, ...]) -> MinutesItemRow:
    return MinutesItemRow(*r[:13], bool(r[13]), tuple(json.loads(r[14])))


@dataclass(frozen=True, slots=True)
class SearchHit:
    meeting_id: str
    meeting_title: str
    entity_kind: str  # 'meeting' or 'segment'
    entity_id: str
    text: str = field(repr=False)  # the original (not normalized) current text
    source: str | None
    start_ms: int | None


class TranscriptStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self._conn = connect(self.path)
            migrate(self._conn)
        except sqlite3.Error as exc:
            raise StorageError(_code(exc)) from None
        with self._tx() as c:
            now = utc_now()
            c.execute("INSERT OR IGNORE INTO users(id, display_name, created_at) VALUES (?, ?, ?)",
                      (OWNER_ID, "Local user", now))
            c.execute("INSERT OR IGNORE INTO agents(id, owner_id, label, created_at) VALUES (?, ?, ?, ?)",
                      (AGENT_ID, OWNER_ID, "This computer", now))

    def close(self, *, wait_s: float | None = None) -> bool:
        """Close the database. With `wait_s`, gives up (returns False) if another
        thread is still inside a store operation, e.g. a save stalled on the disk."""
        if not self._lock.acquire(timeout=-1 if wait_s is None else wait_s):
            return False
        try:
            self._conn.close()
            return True
        finally:
            self._lock.release()

    @contextmanager
    def held(self, wait_s: float | None = None) -> Iterator[None]:
        """Hold the store across several calls, so reads between them see one state.
        Every write goes through this object, so nothing changes while it is held.
        With `wait_s`, waits at most that long for another thread's operation and
        raises StorageError('busy') instead of blocking (used on the UI thread)."""
        if not self._lock.acquire(timeout=-1 if wait_s is None else wait_s):
            raise StorageError("busy")
        try:
            yield
        finally:
            self._lock.release()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                raise StorageError(_code(exc)) from None
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except sqlite3.Error as exc:
                self._rollback()
                raise StorageError(_code(exc)) from None
            except BaseException:
                self._rollback()
                raise

    def _rollback(self) -> None:
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass  # already rolled back by SQLite (e.g. after SQLITE_FULL)

    def _read(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with self._lock:
            try:
                return self._conn.execute(sql, params).fetchall()
            except sqlite3.Error as exc:
                raise StorageError(_code(exc)) from None

    # ------------------------------------------------------------------ meetings

    def create_meeting(self, meeting_id: str, *, title: str, timezone_name: str, started_at: str | None = None) -> None:
        now = utc_now()
        with self._tx() as c:
            c.execute(
                "INSERT INTO meetings(id, owner_id, agent_id, title, started_at, timezone, status, data_mode,"
                " notice_json, created_at) VALUES (?, ?, ?, ?, ?, ?, 'starting', 'local', '{}', ?)",
                (meeting_id, OWNER_ID, AGENT_ID, title, started_at or now, timezone_name, now),
            )
            self._index(c, meeting_id, "meeting", meeting_id, title)

    def set_status(self, meeting_id: str, status: str, *, ended: bool = False) -> None:
        with self._tx() as c:
            if ended:
                c.execute("UPDATE meetings SET status = ?, ended_at = ? WHERE id = ?", (status, utc_now(), meeting_id))
            else:
                c.execute("UPDATE meetings SET status = ? WHERE id = ?", (status, meeting_id))

    def rename_meeting(self, meeting_id: str, title: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE meetings SET title = ? WHERE id = ?", (title, meeting_id))
            self._index(c, meeting_id, "meeting", meeting_id, title)

    def save_epoch(self, meeting_id: str, *, source: str, epoch: int, device_id: str, device_label: str,
                   native_rate: int, native_channels: int, start_ms: int, end_ms: int | None,
                   config: dict[str, Any] | None = None) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO source_epochs(meeting_id, source, epoch, device_id, device_label, native_rate,"
                " native_channels, start_ms, end_ms, config_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(meeting_id, source, epoch) DO UPDATE SET end_ms = excluded.end_ms",
                (meeting_id, source, epoch, device_id, device_label, native_rate, native_channels, start_ms,
                 end_ms, json.dumps(config or {})),
            )

    def close_epoch(self, meeting_id: str, source: str, epoch: int, end_ms: int) -> None:
        with self._tx() as c:
            c.execute("UPDATE source_epochs SET end_ms = MAX(start_ms, ?) WHERE meeting_id = ? AND source = ?"
                      " AND epoch = ?", (end_ms, meeting_id, source, epoch))

    def epochs(self, meeting_id: str) -> list[tuple[str, int]]:
        return [(r[0], r[1]) for r in self._read(
            "SELECT source, epoch FROM source_epochs WHERE meeting_id = ? ORDER BY start_ms", (meeting_id,))]

    # ------------------------------------------------------------- transcript text

    def add_final(self, meeting_id: str, *, source: str, epoch: int, start_ms: int, end_ms: int, text: str,
                  language: str | None, engine_id: str, model_hash: str, flags: list[str] | None = None,
                  segment_id: str | None = None) -> str:
        """Save one recognized final as a new segment with its ASR revision 1."""
        segment_id = segment_id or str(uuid.uuid4())
        with self._tx() as c:
            c.execute(
                "INSERT INTO segments(id, meeting_id, source, epoch, start_ms, end_ms, current_revision, engine_id,"
                " model_hash, timing_quality, flags_json) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, 'chunk', ?)",
                (segment_id, meeting_id, source, epoch, start_ms, max(start_ms, end_ms), engine_id, model_hash,
                 json.dumps(flags or [])),
            )
            c.execute(
                "INSERT INTO segment_revisions(segment_id, meeting_id, revision, text, language, origin, edited_at)"
                " VALUES (?, ?, 1, ?, ?, 'asr', ?)",
                (segment_id, meeting_id, text, language or "unknown", utc_now()),
            )
            self._index(c, meeting_id, "segment", segment_id, text)
            self._bump(c, meeting_id)
        return segment_id

    def correct_segment(self, segment_id: str, text: str) -> int:
        """Append a user revision; the original ASR revision stays as provenance."""
        with self._tx() as c:
            row = c.execute("SELECT meeting_id, current_revision FROM segments WHERE id = ?", (segment_id,)).fetchone()
            if row is None:
                raise KeyError("segment not found")
            meeting_id, current = row
            language = c.execute("SELECT language FROM segment_revisions WHERE segment_id = ? AND revision = ?",
                                 (segment_id, current)).fetchone()[0]
            revision = current + 1
            c.execute(
                "INSERT INTO segment_revisions(segment_id, meeting_id, revision, text, language, origin, edited_at)"
                " VALUES (?, ?, ?, ?, ?, 'user', ?)",
                (segment_id, meeting_id, revision, text, language, utc_now()),
            )
            c.execute("UPDATE segments SET current_revision = ? WHERE id = ?", (revision, segment_id))
            self._index(c, meeting_id, "segment", segment_id, text)
            self._bump(c, meeting_id)
        return revision

    def redact_segment(self, segment_id: str) -> None:
        """Remove a segment's text in every revision and from the index. The interval
        stays visible in the coverage as a content-free 'segment_redacted' notice."""
        with self._tx() as c:
            row = c.execute("SELECT meeting_id, source, start_ms, end_ms FROM segments WHERE id = ?",
                            (segment_id,)).fetchone()
            if row is None:
                raise KeyError("segment not found")
            meeting_id, source, start_ms, end_ms = row
            c.execute("DELETE FROM search_documents WHERE entity_kind = 'segment' AND entity_id = ?", (segment_id,))
            c.execute("DELETE FROM segment_revisions WHERE segment_id = ?", (segment_id,))
            c.execute("DELETE FROM segments WHERE id = ?", (segment_id,))
            self._timeline(c, meeting_id, "notice", "segment_redacted", start_ms, end_ms, source, {})
            self._bump(c, meeting_id)
            # Any redaction invalidates every minutes version of the meeting and
            # deletes their generated text/actions in the same transaction.
            self._invalidate_minutes(c, meeting_id, "transcript_redacted")
        self.scrub()  # MaintenanceError after this point means the text was removed

    def delete_meeting(self, meeting_id: str) -> None:
        """Delete the meeting with all its text, revisions, index entries and events.
        StorageError: nothing was deleted; MaintenanceError: it was deleted."""
        with self._tx() as c:
            self._delete_rows(c, meeting_id)
        try:
            self.scrub()
        except MaintenanceError as exc:
            raise MaintenanceError(exc.code, (meeting_id,)) from None

    def expired_meetings(self, before: str, *, exclude: Collection[str] = (), limit: int = MAX_PAGE) -> list[str]:
        """Finished meetings (stopped or interrupted) that ended before `before` (UTC
        ISO-8601), oldest first; a meeting with no recorded end counts from its start.
        Never meetings still running/being saved, nor those in `exclude`. An unreadable
        timestamp compares as NULL, so it never expires."""
        limit = max(1, min(limit, MAX_PAGE))
        rows = self._read(
            "SELECT id FROM meetings WHERE owner_id = ? AND status IN ('stopped', 'interrupted')"
            " AND julianday(COALESCE(ended_at, started_at)) < julianday(?)"
            " ORDER BY COALESCE(ended_at, started_at), id LIMIT ?",
            (OWNER_ID, before, limit + len(exclude)),
        )
        return [r[0] for r in rows if r[0] not in exclude][:limit]

    def purge_expired(self, before: str, *, exclude: Collection[str] = (), limit: int = 20) -> list[str]:
        """Retention cleanup: delete up to `limit` meetings from `expired_meetings`, in
        one transaction (all or nothing: StorageError means none was deleted). Returns
        the deleted ids. The selection is repeated inside the transaction, so a meeting
        that changed in between is judged on its current state. If the deletion
        committed but the maintenance after it failed, MaintenanceError carries the
        deleted ids."""
        with self._tx() as c:
            ids = self.expired_meetings(before, exclude=exclude, limit=limit)
            for meeting_id in ids:
                self._delete_rows(c, meeting_id)
        if ids:
            try:
                self.scrub()
            except MaintenanceError as exc:
                raise MaintenanceError(exc.code, tuple(ids)) from None
        return ids

    @staticmethod
    def _delete_rows(c: sqlite3.Connection, meeting_id: str) -> None:
        c.execute("DELETE FROM minutes_items WHERE meeting_id = ?", (meeting_id,))
        c.execute("DELETE FROM minutes_versions WHERE meeting_id = ?", (meeting_id,))
        c.execute("DELETE FROM search_documents WHERE meeting_id = ?", (meeting_id,))
        c.execute("DELETE FROM segment_revisions WHERE meeting_id = ?", (meeting_id,))
        c.execute("DELETE FROM segments WHERE meeting_id = ?", (meeting_id,))
        c.execute("DELETE FROM timeline_events WHERE meeting_id = ?", (meeting_id,))
        c.execute("DELETE FROM source_epochs WHERE meeting_id = ?", (meeting_id,))
        c.execute("DELETE FROM speakers WHERE meeting_id = ?", (meeting_id,))
        c.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))

    def scrub(self) -> None:
        """Merge the FTS index (dropping deleted tokens) and fold the WAL back into the
        database so deleted text does not stay in -wal or in old database pages. Runs
        after every deletion and redaction; call it again to retry after a
        MaintenanceError. If it cannot finish, including when another connection's
        read snapshot keeps the checkpoint 'busy', a marker is saved so the retry is
        remembered after a restart (`maintenance_pending`). The marker is cleared only
        after a checkpoint has succeeded, so a crash at any point leaves it set and the
        retry is repeated. Best effort: storage hardware, snapshots and earlier
        exports are outside the app's control."""
        with self._lock:
            try:
                self._conn.execute("INSERT INTO search_fts(search_fts) VALUES('optimize')")
                busy = self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
                if not busy and self.maintenance_pending():
                    with self._tx() as c:
                        c.execute("DELETE FROM settings WHERE owner_id = ? AND key = ?", (OWNER_ID, MAINTENANCE_KEY))
                    # Folds back the marker's own small write; the deleted text is already
                    # out of the WAL, so a reader blocking this one leaves nothing owed.
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            except sqlite3.Error as exc:
                self._remember_maintenance()
                raise MaintenanceError(_code(exc)) from None
            except StorageError as exc:
                self._remember_maintenance()
                raise MaintenanceError(exc.code) from None
            if busy:  # another reader still needs the old pages: they were not overwritten
                self._remember_maintenance()
                raise MaintenanceError("busy")

    def maintenance_pending(self) -> bool:
        """Whether an earlier tidy-up after a deletion or redaction did not finish."""
        return bool(self.get_setting(MAINTENANCE_KEY))

    def _remember_maintenance(self) -> None:
        try:
            self.set_setting(MAINTENANCE_KEY, True)
        except StorageError:
            pass  # cannot be saved either (e.g. disk full): the caller still gets MaintenanceError

    # ------------------------------------------------------------------ settings

    def get_setting(self, key: str) -> Any:
        """A stored setting (parsed JSON), or None if it was never set."""
        rows = self._read("SELECT value_json FROM settings WHERE owner_id = ? AND key = ?", (OWNER_ID, key))
        return json.loads(rows[0][0]) if rows else None

    def set_setting(self, key: str, value: Any) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO settings(owner_id, key, value_json) VALUES (?, ?, ?)"
                      " ON CONFLICT(owner_id, key) DO UPDATE SET value_json = excluded.value_json",
                      (OWNER_ID, key, json.dumps(value)))

    # ------------------------------------------------------------------ timeline

    def add_timeline(self, meeting_id: str, kind: str, code: str, start_ms: int, end_ms: int | None = None,
                     source: str | None = None, metadata: dict[str, Any] | None = None) -> str:
        with self._tx() as c:
            return self._timeline(c, meeting_id, kind, code, start_ms, end_ms, source, metadata or {})

    def recover_unfinished(self) -> list[str]:
        """At startup: meetings left running by a crash or forced exit become
        'interrupted', with a notice at the last known transcript time. Capture is
        never reopened and lost audio is not reconstructed."""
        recovered = []
        with self._tx() as c:
            rows = c.execute(
                f"SELECT id, status FROM meetings WHERE status IN ({','.join('?' * len(UNFINISHED))})", UNFINISHED
            ).fetchall()
            for meeting_id, status in rows:
                last = c.execute(
                    "SELECT MAX(m) FROM (SELECT MAX(end_ms) AS m FROM segments WHERE meeting_id = ?1"
                    " UNION ALL SELECT MAX(COALESCE(end_ms, start_ms)) FROM timeline_events WHERE meeting_id = ?1)",
                    (meeting_id,),
                ).fetchone()[0] or 0
                c.execute("UPDATE meetings SET status = 'interrupted' WHERE id = ?", (meeting_id,))
                self._timeline(c, meeting_id, "error", "app_ended_during_meeting", last, None, None,
                               {"status_at_end": status})
                recovered.append(meeting_id)
        return recovered

    # --------------------------------------------------------------------- reads

    def list_meetings(self, *, limit: int = 50, before: str | None = None) -> list[MeetingRow]:
        limit = max(1, min(limit, MAX_PAGE))
        rows = self._read(
            "SELECT m.id, m.title, m.started_at, m.ended_at, m.timezone, m.status, m.transcript_revision,"
            " (SELECT COUNT(*) FROM segments s WHERE s.meeting_id = m.id)"
            " FROM meetings m WHERE m.owner_id = ? AND (? IS NULL OR m.started_at < ?)"
            " ORDER BY m.started_at DESC, m.id LIMIT ?",
            (OWNER_ID, before, before, limit),
        )
        return [MeetingRow(*r) for r in rows]

    def meeting(self, meeting_id: str) -> MeetingRow | None:
        rows = self._read(
            "SELECT m.id, m.title, m.started_at, m.ended_at, m.timezone, m.status, m.transcript_revision,"
            " (SELECT COUNT(*) FROM segments s WHERE s.meeting_id = m.id) FROM meetings m"
            " WHERE m.id = ? AND m.owner_id = ?",
            (meeting_id, OWNER_ID),
        )
        return MeetingRow(*rows[0]) if rows else None

    def snapshot(self, meeting_id: str, *, wait_s: float | None = None) -> MeetingSnapshot | None:
        """The meeting, its current segment text and its timeline as one consistent
        revision, or None if it does not exist. See `held` for `wait_s`."""
        with self.held(wait_s):
            row = self.meeting(meeting_id)
            if row is None:
                return None
            return MeetingSnapshot(row, tuple(self.segments(meeting_id)), tuple(self.timeline(meeting_id)))

    def segments(self, meeting_id: str) -> list[SegmentView]:
        rows = self._read(
            "SELECT s.id, s.source, s.epoch, s.start_ms, s.end_ms, s.current_revision, r.text, r.language,"
            " r.origin, s.flags_json FROM segments s JOIN segment_revisions r"
            " ON r.segment_id = s.id AND r.revision = s.current_revision"
            " WHERE s.meeting_id = ? ORDER BY s.start_ms, s.source, s.id",
            (meeting_id,),
        )
        return [SegmentView(*r[:9], tuple(json.loads(r[9]))) for r in rows]

    def revisions(self, segment_id: str) -> list[RevisionView]:
        rows = self._read(
            "SELECT revision, text, language, origin, edited_at FROM segment_revisions WHERE segment_id = ?"
            " ORDER BY revision",
            (segment_id,),
        )
        return [RevisionView(*r) for r in rows]

    def timeline(self, meeting_id: str) -> list[TimelineRow]:
        rows = self._read(
            "SELECT id, kind, source, start_ms, end_ms, code, metadata_json FROM timeline_events"
            " WHERE meeting_id = ? ORDER BY start_ms, id",
            (meeting_id,),
        )
        return [TimelineRow(*r[:6], json.loads(r[6])) for r in rows]

    def search(self, text: str, *, limit: int = 50) -> list[SearchHit]:
        """Literal, bounded search over meeting titles and current segment text."""
        query = literal_query(text)
        if query is None:
            return []
        rows = self._read(
            "SELECT d.meeting_id, m.title, d.entity_kind, d.entity_id,"
            " CASE d.entity_kind WHEN 'segment' THEN r.text ELSE m.title END, s.source, s.start_ms"
            " FROM search_fts JOIN search_documents d ON d.id = search_fts.rowid"
            " JOIN meetings m ON m.id = d.meeting_id AND m.owner_id = d.owner_id"
            " LEFT JOIN segments s ON d.entity_kind = 'segment' AND s.id = d.entity_id"
            " LEFT JOIN segment_revisions r ON r.segment_id = s.id AND r.revision = s.current_revision"
            " WHERE search_fts MATCH ? AND d.owner_id = ? AND d.entity_kind IN ('meeting', 'segment')"
            " ORDER BY bm25(search_fts), d.id LIMIT ?",
            (query, OWNER_ID, max(1, min(limit, MAX_PAGE))),
        )
        return [SearchHit(*r) for r in rows]

    # ------------------------------------------------------------------- minutes
    # Generated minutes are derived from one saved transcript revision. Only segment
    # ids/revisions are kept as the snapshot; minutes text is not indexed for search.

    def redaction_count(self, meeting_id: str) -> int:
        """How many redactions the meeting has had (content-free timeline notices)."""
        return self._read("SELECT COUNT(*) FROM timeline_events WHERE meeting_id = ? AND code = 'segment_redacted'",
                          (meeting_id,))[0][0]

    def save_minutes_version(self, meeting_id: str, *, transcript_revision: int, snapshot_hash: str,
                             snapshot_refs: list[tuple[str, int]], redactions_at_snapshot: int, provider: str,
                             model_id: str, prompt_version: str, schema_version: int, output_language: str,
                             input_tokens: int | None, output_tokens: int | None, requests: int, parts: int,
                             incomplete: bool, warnings: list[str], items: list[dict[str, Any]]) -> str:
        """Save a new draft version (always a new version number; never overwrites).
        Refused with MinutesRejected when the snapshot is no longer valid: the meeting
        was deleted, a redaction happened after the snapshot, or a snapshot segment
        revision no longer exists. This rejects late in-flight results."""
        version_id = str(uuid.uuid4())
        now = utc_now()
        with self._tx() as c:
            if c.execute("SELECT 1 FROM meetings WHERE id = ? AND owner_id = ?", (meeting_id, OWNER_ID)).fetchone() is None:
                raise MinutesRejected("meeting_deleted")
            redactions = c.execute("SELECT COUNT(*) FROM timeline_events WHERE meeting_id = ?"
                                   " AND code = 'segment_redacted'", (meeting_id,)).fetchone()[0]
            if redactions != redactions_at_snapshot:
                raise MinutesRejected("transcript_redacted")
            for segment_id, revision in snapshot_refs:
                if c.execute("SELECT 1 FROM segment_revisions WHERE segment_id = ? AND meeting_id = ? AND revision = ?",
                             (segment_id, meeting_id, revision)).fetchone() is None:
                    raise MinutesRejected("evidence_missing")
            number = c.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM minutes_versions WHERE meeting_id = ?",
                               (meeting_id,)).fetchone()[0]
            c.execute(
                f"INSERT INTO minutes_versions({_VERSION_COLUMNS}, snapshot_refs_json, redactions_at_snapshot)"
                " VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', 0, ?, ?, 0, NULL, ?, ?)",
                (version_id, meeting_id, number, transcript_revision, snapshot_hash, provider, model_id,
                 prompt_version, schema_version, output_language, input_tokens, output_tokens, requests, parts,
                 int(incomplete), json.dumps(warnings, ensure_ascii=False), now, now,
                 json.dumps([list(r) for r in snapshot_refs]), redactions_at_snapshot),
            )
            for position, item in enumerate(items):
                self._insert_item(c, version_id, meeting_id, position, item)
        return version_id

    def minutes_versions(self, meeting_id: str) -> list[MinutesVersionRow]:
        """All versions of a meeting, oldest first. A redaction made by a build that did
        not know about minutes is detected here and invalidates the versions."""
        query = (f"SELECT {_VERSION_COLUMNS}, redactions_at_snapshot FROM minutes_versions"
                 " WHERE meeting_id = ? ORDER BY version")
        with self._lock:
            rows = self._read(query, (meeting_id,))
            if any(not r[21] for r in rows):
                redactions = self.redaction_count(meeting_id)
                if any(not r[21] and r[23] != redactions for r in rows):
                    with self._tx() as c:
                        self._invalidate_minutes(c, meeting_id, "transcript_redacted")
                    try:
                        self.scrub()
                    except MaintenanceError:
                        pass  # the text is deleted; the tidy-up is remembered and retried
                    rows = self._read(query, (meeting_id,))
            return [_version_row(r[:23]) for r in rows]

    def minutes_version(self, version_id: str) -> MinutesVersionRow | None:
        rows = self._read("SELECT meeting_id FROM minutes_versions WHERE id = ?", (version_id,))
        if not rows:
            return None
        return next((v for v in self.minutes_versions(rows[0][0]) if v.id == version_id), None)

    def minutes_snapshot_refs(self, version_id: str) -> list[tuple[str, int]]:
        rows = self._read("SELECT snapshot_refs_json FROM minutes_versions WHERE id = ? AND invalidated = 0",
                          (version_id,))
        return [(r[0], r[1]) for r in json.loads(rows[0][0])] if rows else []

    def minutes_items(self, version_id: str) -> list[MinutesItemRow]:
        """Items of a valid version; an invalidated version has none (they were deleted)."""
        rows = self._read(
            f"SELECT {', '.join('i.' + c.strip() for c in _ITEM_COLUMNS.split(','))} FROM minutes_items i"
            " JOIN minutes_versions v ON v.id = i.version_id"
            " WHERE i.version_id = ? AND v.invalidated = 0 ORDER BY i.kind, i.position",
            (version_id,))
        return [_item_row(r) for r in rows]

    def minutes_item(self, item_id: str) -> MinutesItemRow | None:
        rows = self._read(
            f"SELECT {', '.join('i.' + c.strip() for c in _ITEM_COLUMNS.split(','))} FROM minutes_items i"
            " JOIN minutes_versions v ON v.id = i.version_id WHERE i.id = ? AND v.invalidated = 0", (item_id,))
        return _item_row(rows[0]) if rows else None

    def update_minutes_item(self, item_id: str, changes: dict[str, Any]) -> None:
        """Edit an item of a draft/in-review version in place (marks it 'your edits')."""
        unknown = set(changes) - set(MINUTES_ITEM_FIELDS)
        if unknown:
            raise ValueError("unknown minutes item field")
        with self._tx() as c:
            version_id = self._editable_version(c, item_id=item_id)
            for name, value in changes.items():
                c.execute(f"UPDATE minutes_items SET {name} = ? WHERE id = ?",
                          (int(value) if name == "reviewed" else value, item_id))
            self._touch_version(c, version_id)

    def add_minutes_item(self, version_id: str, kind: str, fields: dict[str, Any]) -> str:
        """A user-added item (origin 'user', no transcript evidence)."""
        with self._tx() as c:
            self._editable_version(c, version_id=version_id)
            meeting_id, position = c.execute(
                "SELECT v.meeting_id, (SELECT COALESCE(MAX(position), -1) + 1 FROM minutes_items"
                " WHERE version_id = v.id AND kind = ?) FROM minutes_versions v WHERE v.id = ?",
                (kind, version_id)).fetchone()
            item_id = self._insert_item(c, version_id, meeting_id, position,
                                        {**fields, "kind": kind, "origin": "user", "evidence": []})
            self._touch_version(c, version_id)
        return item_id

    def delete_minutes_item(self, item_id: str) -> None:
        with self._tx() as c:
            version_id = self._editable_version(c, item_id=item_id)
            c.execute("DELETE FROM minutes_items WHERE id = ?", (item_id,))
            self._touch_version(c, version_id)

    def set_minutes_state(self, version_id: str, state: str) -> None:
        """draft -> review -> approved (review -> draft allowed). Approved is final:
        changes go through `copy_minutes_version` as a new draft."""
        allowed = {("draft", "review"), ("review", "draft"), ("review", "approved")}
        with self._tx() as c:
            row = c.execute("SELECT state, invalidated FROM minutes_versions WHERE id = ?", (version_id,)).fetchone()
            if row is None:
                raise MinutesRejected("not_found")
            if row[1]:
                raise MinutesRejected("invalidated")
            if (row[0], state) not in allowed:
                raise MinutesRejected("bad_state")
            c.execute("UPDATE minutes_versions SET state = ?, updated_at = ? WHERE id = ?",
                      (state, utc_now(), version_id))

    def copy_minutes_version(self, version_id: str) -> str:
        """A new draft version with a copy of this version's items (Edit as new draft)."""
        new_id = str(uuid.uuid4())
        now = utc_now()
        with self._tx() as c:
            row = c.execute("SELECT meeting_id, version, invalidated FROM minutes_versions WHERE id = ?",
                            (version_id,)).fetchone()
            if row is None:
                raise MinutesRejected("not_found")
            if row[2]:
                raise MinutesRejected("invalidated")
            meeting_id, parent = row[0], row[1]
            number = c.execute("SELECT MAX(version) + 1 FROM minutes_versions WHERE meeting_id = ?",
                               (meeting_id,)).fetchone()[0]
            c.execute(
                "INSERT INTO minutes_versions(id, meeting_id, version, parent_version, transcript_revision,"
                " snapshot_hash, snapshot_refs_json, redactions_at_snapshot, provider, model_id, prompt_version,"
                " schema_version, output_language, input_tokens, output_tokens, requests, parts, incomplete,"
                " warnings_json, state, user_edited, created_at, updated_at, invalidated, invalidated_reason)"
                " SELECT ?, meeting_id, ?, ?, transcript_revision, snapshot_hash, snapshot_refs_json,"
                " redactions_at_snapshot, provider, model_id, prompt_version, schema_version, output_language,"
                " input_tokens, output_tokens, requests, parts, incomplete, warnings_json, 'draft', user_edited,"
                " ?, ?, 0, NULL FROM minutes_versions WHERE id = ?",
                (new_id, number, parent, now, now, version_id))
            for r in c.execute(f"SELECT {_ITEM_COLUMNS} FROM minutes_items WHERE version_id = ?",
                               (version_id,)).fetchall():
                item = _item_row(r)
                fields = {name: getattr(item, name) for name in MINUTES_ITEM_FIELDS}
                self._insert_item(c, new_id, meeting_id, item.position,
                                  {**fields, "kind": item.kind, "origin": item.origin,
                                   "evidence": list(item.evidence)})
        return new_id

    @staticmethod
    def _editable_version(c: sqlite3.Connection, *, item_id: str | None = None, version_id: str | None = None) -> str:
        if item_id is not None:
            row = c.execute("SELECT v.id, v.state, v.invalidated FROM minutes_items i JOIN minutes_versions v"
                            " ON v.id = i.version_id WHERE i.id = ?", (item_id,)).fetchone()
        else:
            row = c.execute("SELECT id, state, invalidated FROM minutes_versions WHERE id = ?",
                            (version_id,)).fetchone()
        if row is None:
            raise MinutesRejected("not_found")
        if row[2]:
            raise MinutesRejected("invalidated")
        if row[1] == "approved":
            raise MinutesRejected("read_only")
        return row[0]

    @staticmethod
    def _touch_version(c: sqlite3.Connection, version_id: str) -> None:
        c.execute("UPDATE minutes_versions SET user_edited = 1, updated_at = ? WHERE id = ?", (utc_now(), version_id))

    @staticmethod
    def _insert_item(c: sqlite3.Connection, version_id: str, meeting_id: str, position: int,
                     item: dict[str, Any]) -> str:
        item_id = str(uuid.uuid4())
        c.execute(
            f"INSERT INTO minutes_items({_ITEM_COLUMNS}, meeting_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, version_id, item["kind"], position, item.get("text") or "", item.get("detail"),
             item.get("status"), item.get("owner"), item.get("due_date"), item.get("due_date_text"),
             item.get("date_status"), item.get("commitment"), item.get("origin", "model"),
             int(bool(item.get("reviewed"))), json.dumps(item.get("evidence") or [], ensure_ascii=False), meeting_id),
        )
        return item_id

    @staticmethod
    def _invalidate_minutes(c: sqlite3.Connection, meeting_id: str, reason: str) -> None:
        """Delete all generated/edited minutes text and actions of the meeting and keep
        only content-free lineage marked invalidated."""
        c.execute("DELETE FROM minutes_items WHERE meeting_id = ?", (meeting_id,))
        c.execute("UPDATE minutes_versions SET invalidated = 1, invalidated_reason = ?, warnings_json = '[]',"
                  " snapshot_refs_json = '[]', snapshot_hash = '', updated_at = ? WHERE meeting_id = ?"
                  " AND invalidated = 0", (reason, utc_now(), meeting_id))

    # ------------------------------------------------------------------- helpers

    def _index(self, c: sqlite3.Connection, meeting_id: str, kind: str, entity_id: str, text: str) -> None:
        c.execute(
            "INSERT INTO search_documents(owner_id, meeting_id, entity_kind, entity_id, normalized_text)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT(entity_kind, entity_id)"
            " DO UPDATE SET normalized_text = excluded.normalized_text",
            (OWNER_ID, meeting_id, kind, entity_id, normalize(text)),
        )

    @staticmethod
    def _bump(c: sqlite3.Connection, meeting_id: str) -> None:
        c.execute("UPDATE meetings SET transcript_revision = transcript_revision + 1 WHERE id = ?", (meeting_id,))

    @staticmethod
    def _timeline(c: sqlite3.Connection, meeting_id: str, kind: str, code: str, start_ms: int,
                  end_ms: int | None, source: str | None, metadata: dict[str, Any]) -> str:
        event_id = str(uuid.uuid4())
        c.execute(
            "INSERT INTO timeline_events(id, meeting_id, kind, source, start_ms, end_ms, code, metadata_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, meeting_id, kind, source, max(0, start_ms),
             None if end_ms is None else max(max(0, start_ms), end_ms), code, json.dumps(metadata)),
        )
        return event_id
