"""Transcript store: schema, revisions, literal bilingual search, redaction,
deletion, atomic failure and restart recovery. Real SQLite files in a temp folder."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from meeting_minutes.storage.schema import SCHEMA_VERSION, SchemaError, connect
from meeting_minutes.storage.search import MAX_QUERY_CHARS, QueryError, literal_query, normalize
from meeting_minutes.storage.store import MaintenanceError, StorageError, TranscriptStore

V1_TABLES = {"users", "agents", "meetings", "speakers", "source_epochs", "segments", "segment_revisions",
             "timeline_events", "settings", "search_documents", "search_fts"}
CANARY = "zebracanaryquartz"


def store_at(tmp_path: Path) -> TranscriptStore:
    return TranscriptStore(tmp_path / "meetings.db")


def meeting(store: TranscriptStore, meeting_id: str = "m1", title: str = "Weekly sync") -> str:
    store.create_meeting(meeting_id, title=title, timezone_name="Africa/Cairo")
    for source in ("mic", "system"):
        store.save_epoch(meeting_id, source=source, epoch=1, device_id=f"dev-{source}", device_label=source,
                         native_rate=48_000, native_channels=1, start_ms=0, end_ms=None)
    return meeting_id


def final(store: TranscriptStore, text: str, *, meeting_id: str = "m1", source: str = "system", start: int = 0,
          language: str = "en") -> str:
    return store.add_final(meeting_id, source=source, epoch=1, start_ms=start, end_ms=start + 900, text=text,
                           language=language, engine_id="fake-engine", model_hash="0" * 64)


def file_bytes(tmp_path: Path) -> bytes:
    return b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.name.startswith("meetings.db"))


def test_v1_migration_creates_only_the_v1_subset_with_foreign_keys(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    conn = connect(store.path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table')")}
    assert V1_TABLES <= tables
    assert not tables & {"snapshots", "templates", "template_versions", "minutes", "actions", "jobs", "sync_outbox"}
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 1
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    store.close()
    TranscriptStore(tmp_path / "meetings.db").close()  # reopening does not migrate again


def test_a_newer_unknown_schema_is_refused(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "meetings.db")
    conn.execute("PRAGMA user_version = 9")
    conn.close()
    with pytest.raises((SchemaError, StorageError)):
        TranscriptStore(tmp_path / "meetings.db")


def test_a_segment_needs_its_capture_epoch(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.create_meeting("m1", title="t", timezone_name="UTC")
    with pytest.raises(StorageError):
        final(store, "orphan")
    assert store.segments("m1") == [] and store.meeting("m1").transcript_revision == 0


def test_finals_are_ordered_by_time_with_source_and_original_script(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    meeting(store)
    final(store, "مرحبا بكم في الاجتماع", source="system", start=2_000, language="ar")
    final(store, "Good morning everyone", source="mic", start=500)
    rows = store.segments("m1")
    assert [(r.source, r.start_ms, r.text, r.language, r.origin, r.revision) for r in rows] == [
        ("mic", 500, "Good morning everyone", "en", "asr", 1),
        ("system", 2_000, "مرحبا بكم في الاجتماع", "ar", "asr", 1),
    ]
    assert store.meeting("m1").transcript_revision == 2


def test_search_normalizes_arabic_and_english_and_is_literal(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    meeting(store, title="Budget review")
    final(store, "مُراجعةُ الميزانيّة لعام ٢٠٢٦ في قسم إدارة المشروع", language="ar")
    final(store, "The DEADLINE is Thursday", start=1_000)
    assert len(store.search("مراجعة الميزانية")) == 1  # diacritics and shadda ignored
    assert len(store.search("2026")) == 1  # Arabic-Indic digits
    assert len(store.search("ادارة")) == 1  # alef with hamza below
    assert [h.text for h in store.search("deadline thursday")] == ["The DEADLINE is Thursday"]  # original kept
    assert [h.entity_kind for h in store.search("budget")] == ["meeting"]
    for hostile in ('budget OR deadline', '"', "*", "NEAR(the deadline)", "deadline:", "^the", "-x"):
        store.search(hostile)  # never an FTS syntax error: every token is literal
    assert store.search("budget OR deadline") == []  # OR is a word here, and no text has it
    with pytest.raises(QueryError):
        store.search("x" * (MAX_QUERY_CHARS + 1))


def test_normalization_keeps_ta_marbuta_and_alef_maqsura_distinct() -> None:
    assert normalize("مدرسة") != normalize("مدرسه")
    assert normalize("على") != normalize("علي")
    assert literal_query("   ") is None


def test_a_correction_appends_a_user_revision_and_reindexes_only_current_text(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    meeting(store)
    segment = final(store, "the budget is fine")
    assert store.correct_segment(segment, "the forecast is fine") == 2
    assert [(r.revision, r.origin, r.text) for r in store.revisions(segment)] == [
        (1, "asr", "the budget is fine"), (2, "user", "the forecast is fine")]
    [row] = store.segments("m1")
    assert (row.text, row.origin, row.revision) == ("the forecast is fine", "user", 2)
    assert store.search("budget") == [] and len(store.search("forecast")) == 1
    assert store.meeting("m1").transcript_revision == 2


def test_redaction_removes_every_revision_and_index_entry_from_the_files(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    meeting(store)
    segment = final(store, f"the secret word is {CANARY}", start=3_000)
    store.correct_segment(segment, f"corrected {CANARY} again")
    final(store, "keep this sentence", start=5_000)
    assert CANARY.encode() in file_bytes(tmp_path)
    store.redact_segment(segment)
    assert [s.text for s in store.segments("m1")] == ["keep this sentence"]
    assert store.revisions(segment) == [] and store.search(CANARY) == []
    [notice] = store.timeline("m1")
    assert (notice.kind, notice.code, notice.start_ms, notice.end_ms, notice.metadata) == (
        "notice", "segment_redacted", 3_000, 3_900, {})
    assert CANARY.encode() not in file_bytes(tmp_path)  # database and WAL, while the app is still open
    store.close()
    assert CANARY.encode() not in file_bytes(tmp_path)


def test_deleting_a_meeting_removes_its_text_index_and_events_from_the_files(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    meeting(store, "m1", title=f"Title {CANARY}")
    final(store, f"spoken {CANARY}")
    store.add_timeline("m1", "gap", "stt_backlog", 0, 10, "system", {})
    meeting(store, "m2", title="Other meeting")
    final(store, "other text", meeting_id="m2")
    store.delete_meeting("m1")
    assert store.meeting("m1") is None and store.segments("m1") == [] and store.timeline("m1") == []
    assert store.search(CANARY) == [] and [m.id for m in store.list_meetings()] == ["m2"]
    assert len(store.search("other")) == 2  # the other meeting is untouched
    assert CANARY.encode() not in file_bytes(tmp_path)  # while the app is still open
    store.close()
    assert CANARY.encode() not in file_bytes(tmp_path)


def test_a_full_disk_rolls_the_whole_final_back_with_a_fixed_code(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    meeting(store)
    final(store, "saved before the disk filled")
    pages = store._conn.execute("PRAGMA page_count").fetchone()[0]
    store._conn.execute(f"PRAGMA max_page_count = {pages}")  # SQLite's own SQLITE_FULL
    with pytest.raises(StorageError) as failure:
        for i in range(200):
            final(store, f"a long sentence that will not fit number {i} " * 20, start=10_000 + i)
    assert failure.value.code == "disk_full" and "sentence" not in str(failure.value)
    count = len(store.segments("m1"))
    revisions = store._conn.execute("SELECT COUNT(*) FROM segment_revisions").fetchone()[0]
    documents = store._conn.execute("SELECT COUNT(*) FROM search_documents WHERE entity_kind='segment'").fetchone()[0]
    assert count == revisions == documents == store.meeting("m1").transcript_revision  # no half-saved final
    store._conn.execute("PRAGMA max_page_count = 1073741823")
    final(store, "saved after space was freed", start=99_000)
    assert store.segments("m1")[-1].text == "saved after space was freed"


def test_restart_recovers_running_meetings_as_interrupted_and_keeps_their_text(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    meeting(store)
    store.set_status("m1", "capturing")
    final(store, "said before the crash", start=4_000)
    store.create_meeting("m2", title="finished", timezone_name="UTC")
    store.set_status("m2", "stopped", ended=True)
    store.close()  # the process ends without stopping m1

    reopened = store_at(tmp_path)
    assert reopened.recover_unfinished() == ["m1"]
    assert reopened.meeting("m1").status == "interrupted" and reopened.meeting("m2").status == "stopped"
    assert [s.text for s in reopened.segments("m1")] == ["said before the crash"]
    [event] = reopened.timeline("m1")
    assert (event.kind, event.code, event.start_ms, event.metadata) == (
        "error", "app_ended_during_meeting", 4_900, {"status_at_end": "capturing"})
    assert reopened.recover_unfinished() == []


def test_history_lists_newest_first_with_bounded_pages(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    for i in range(5):
        store.create_meeting(f"m{i}", title=f"meeting {i}", timezone_name="UTC", started_at=f"2026-10-0{i + 1}T10:00:00Z")
    rows = store.list_meetings(limit=2)
    assert [r.id for r in rows] == ["m4", "m3"]
    assert [r.id for r in store.list_meetings(limit=2, before=rows[-1].started_at)] == ["m2", "m1"]
    assert len(store.list_meetings(limit=10_000)) == 5


# ------------------------------------------- revision snapshots, stalled operations, WAL tidy-up


def test_a_snapshot_is_one_revision_and_ui_reads_never_wait_on_a_stalled_operation(tmp_path: Path) -> None:
    import threading
    import time

    store = store_at(tmp_path)
    meeting(store)
    segment = final(store, "the budget is fine")
    store.correct_segment(segment, "مرحبا the forecast is fine")
    snapshot = store.snapshot("m1")
    assert snapshot.meeting.transcript_revision == 2
    assert [(s.text, s.revision, s.origin) for s in snapshot.segments] == [("مرحبا the forecast is fine", 2, "user")]
    assert store.snapshot("missing") is None

    entered, release = threading.Event(), threading.Event()

    def stalled_write() -> None:
        with store._lock:  # as a real store operation stuck on the disk holds it
            entered.set()
            release.wait(5)

    writer = threading.Thread(target=stalled_write)
    writer.start()
    try:
        assert entered.wait(2)
        started = time.monotonic()
        with pytest.raises(StorageError) as busy:
            store.snapshot("m1", wait_s=0.1)
        assert busy.value.code == "busy" and time.monotonic() - started < 1.0
        with pytest.raises(StorageError):
            with store.held(0):
                pass
        assert store.close(wait_s=0.1) is False  # the app can exit without waiting for it
    finally:
        release.set()
        writer.join(5)
    with store.held(0.1):  # free again: several reads see one state
        assert store.meeting("m1").transcript_revision == len(store.revisions(segment))
    assert store.close(wait_s=0.1) is True


def test_a_reader_blocking_the_checkpoint_is_reported_and_retried_after_restart(tmp_path: Path) -> None:
    """Another connection holding a read snapshot makes the WAL
    checkpoint return 'busy' instead of raising. The deletion is committed, but the
    old pages cannot be overwritten yet: that must be reported, remembered across a
    restart, and retried, not passed off as a finished tidy-up."""
    store = store_at(tmp_path)
    meeting(store, "m1")
    final(store, f"spoken {CANARY}")
    meeting(store, "m2", title="Other meeting")
    store.scrub()  # the text is now in the main database file
    reader = sqlite3.connect(store.path)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM segments").fetchall()  # holds a snapshot that still has the text
    with pytest.raises(MaintenanceError) as caught:
        store.delete_meeting("m1")
    assert caught.value.code == "busy" and caught.value.deleted == ("m1",)
    assert store.meeting("m1") is None and store.search(CANARY) == []  # committed: gone from the app
    assert store.maintenance_pending()
    store.close()
    reader.rollback()
    reader.close()

    reopened = store_at(tmp_path)  # restart: the pending tidy-up is remembered
    assert reopened.maintenance_pending()
    reopened.scrub()
    assert not reopened.maintenance_pending()
    assert CANARY.encode() not in file_bytes(tmp_path)
    reopened.close()
    assert CANARY.encode() not in file_bytes(tmp_path)


def _deleted_while_read(tmp_path: Path) -> tuple[TranscriptStore, sqlite3.Connection]:
    """m1 deleted while another connection holds a snapshot that still has its text:
    committed, tidy-up 'busy' and pending."""
    store = store_at(tmp_path)
    meeting(store, "m1")
    final(store, f"spoken {CANARY}")
    store.scrub()
    reader = sqlite3.connect(store.path)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM segments").fetchall()
    with pytest.raises(MaintenanceError, match="busy"):
        store.delete_meeting("m1")
    assert store.maintenance_pending()
    return store, reader


def test_a_retry_stays_pending_while_the_reader_stays_and_clears_after_it_leaves(tmp_path: Path) -> None:
    """Each retry against a reader that still blocks the checkpoint keeps the
    marker; the first retry after the reader has gone clears it and the text."""
    store, reader = _deleted_while_read(tmp_path)
    for _ in range(2):
        with pytest.raises(MaintenanceError, match="busy"):
            store.scrub()
        assert store.maintenance_pending()
    store.close()
    store = store_at(tmp_path)
    with pytest.raises(MaintenanceError, match="busy"):
        store.scrub()
    assert store.maintenance_pending()
    reader.rollback()
    reader.close()
    store.scrub()
    assert not store.maintenance_pending()
    store.close()
    assert CANARY.encode() not in file_bytes(tmp_path)


def test_a_failure_while_clearing_the_marker_keeps_it_pending(tmp_path: Path) -> None:
    """The checkpoint succeeded but removing the marker fails: reported as
    MaintenanceError, the marker stays (a harmless extra retry), and a later retry
    clears it."""
    store, reader = _deleted_while_read(tmp_path)
    reader.rollback()
    reader.close()

    def refuse_marker_delete(action, table, *_):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_DELETE and table == "settings" else sqlite3.SQLITE_OK

    store._conn.set_authorizer(refuse_marker_delete)
    with pytest.raises(MaintenanceError):
        store.scrub()
    assert store.maintenance_pending()
    store._conn.set_authorizer(None)
    store.scrub()
    assert not store.maintenance_pending()
    store.close()
    assert CANARY.encode() not in file_bytes(tmp_path)


@pytest.mark.parametrize("stop_at", ["PRAGMA WAL_CHECKPOINT", "DELETE FROM SETTINGS"])
def test_a_retry_killed_at_any_step_leaves_the_marker_for_the_next_start(tmp_path: Path, stop_at: str) -> None:
    """A real child process retries the pending tidy-up and is killed just
    before its checkpoint, or after the checkpoint but before the marker is removed.
    Either way the next start still knows the retry is owed and finishes it."""
    store, reader = _deleted_while_read(tmp_path)
    store.close()
    if stop_at != "PRAGMA WAL_CHECKPOINT":
        reader.rollback()  # the checkpoint can succeed, so the child reaches the marker
    code = (
        "import os, sys\n"
        "from meeting_minutes.storage.store import TranscriptStore\n"
        "s = TranscriptStore(sys.argv[1])\n"
        "def stop(sql):\n"
        "    if sql.upper().startswith(sys.argv[2]):\n"
        "        os._exit(37)\n"
        "s._conn.set_trace_callback(stop)\n"
        "s.scrub()\n"
        "raise SystemExit(99)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
    child = subprocess.run([sys.executable, "-c", code, str(tmp_path / "meetings.db"), stop_at], env=env,
                           capture_output=True, text=True, timeout=30)
    assert child.returncode == 37, child.stderr
    reader.rollback()
    reader.close()
    store = store_at(tmp_path)
    assert store.meeting("m1") is None and store.search(CANARY) == []
    assert store.maintenance_pending()
    store.scrub()
    assert not store.maintenance_pending()
    store.close()
    assert CANARY.encode() not in file_bytes(tmp_path)
