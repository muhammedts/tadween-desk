"""History search index repair and durable cleanup.

FTS5's own 'secure-delete' option damaged the search index in SQLite 3.42.0 when
indexed rows were deleted or replaced; later saves could then fail. The option is
now off for new stores, an existing store is repaired in place at open (option off
plus a rebuild from `search_documents`, in one transaction), and every deletion or
redaction commits a durable "tidy-up owed" marker that is cleared only after the
index has been rebuilt and the WAL folded back.

Synthetic data and disposable folders only. Meetings have realistic sizes (tens to
hundreds of varied lines): the earlier tests deleted one- or two-line meetings,
which did not trigger the damage.
"""

from __future__ import annotations

import os
import random
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from meeting_minutes.storage import store as store_module
from meeting_minutes.storage.schema import connect, fts_secure_delete_enabled
from meeting_minutes.storage.store import MaintenanceError, StorageError, TranscriptStore

CANARY = "zebracanaryquartz"
FTS_CHECK = "INSERT INTO search_fts(search_fts, rank) VALUES('integrity-check', 1)"
SRC = Path(__file__).resolve().parents[2] / "src"
# A meeting-sized vocabulary: a few thousand distinct words with a natural (Zipf-like)
# frequency spread, English and Arabic script. A tiny vocabulary did not reproduce the
# old damage; this one does on SQLite 3.42.0 with the old FTS option on.
_VOCAB_RNG = random.Random(2026)
EN = ["".join(_VOCAB_RNG.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(_VOCAB_RNG.randint(2, 9)))
      for _ in range(3_000)]
AR = ["".join(_VOCAB_RNG.choice("ابتثجحخدذرزسشصضطظعغفقكلمنهوي") for _ in range(_VOCAB_RNG.randint(2, 7)))
      for _ in range(2_500)]
EN_WEIGHTS = [1.0 / (i + 1) for i in range(len(EN))]
AR_WEIGHTS = [1.0 / (i + 1) for i in range(len(AR))]


def files(folder: Path, name: str = "meetings.db") -> bytes:
    return b"".join(p.read_bytes() for p in folder.iterdir() if p.name.startswith(name))


def fts_ok(store: TranscriptStore) -> bool:
    try:
        with store.held():
            store._conn.execute(FTS_CHECK)
        return True
    except sqlite3.DatabaseError:
        return False


def sentence(rng: random.Random) -> str:
    words, weights = (EN, EN_WEIGHTS) if rng.random() < 0.7 else (AR, AR_WEIGHTS)
    return " ".join(rng.choices(words, weights, k=rng.randint(6, 24)))


def add_meeting(store: TranscriptStore, meeting_id: str, lines: int, rng: random.Random, *,
                canary: bool = False, ended: datetime | None = None) -> list[str]:
    store.create_meeting(meeting_id, title=f"synthetic {meeting_id}", timezone_name="UTC",
                         started_at=None if ended is None else (ended - timedelta(hours=1)).isoformat())
    store.save_epoch(meeting_id, source="system", epoch=1, device_id="fake", device_label="synthetic",
                     native_rate=48_000, native_channels=1, start_ms=0, end_ms=None)
    ids = []
    for i in range(lines):
        text = sentence(rng) + (f" {CANARY}" if canary and i % 10 == 0 else "")
        ids.append(store.add_final(meeting_id, source="system", epoch=1, start_ms=i * 5_000,
                                   end_ms=i * 5_000 + 4_000, text=text, language="en", engine_id="fake",
                                   model_hash="0" * 64))
    store.set_status(meeting_id, "stopped", ended=True)
    if ended is not None:
        with store.held():
            store._conn.execute("UPDATE meetings SET ended_at = ? WHERE id = ?", (ended.isoformat(), meeting_id))
    return ids


def keeps_saving(store: TranscriptStore, rng: random.Random, lines: int = 200) -> None:
    """A new meeting can start and every further line is saved and found."""
    add_meeting(store, "after", lines, rng)
    assert len(store.segments("after")) == lines
    assert any(hit.meeting_id == "after" for hit in store.search("synthetic after", limit=10))


def make_legacy(path: Path) -> None:
    """A store as created before the hotfix: FTS5 'secure-delete' on from the start."""
    TranscriptStore(path).close()
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO search_fts(search_fts, rank) VALUES('secure-delete', 1)")
    conn.commit()
    conn.close()


def table_dump(path: Path) -> dict[str, list[tuple]]:
    """Every saved row outside the search index and the settings table."""
    conn = sqlite3.connect(path)
    try:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'search_fts%'"
            " AND name NOT IN ('settings', 'sqlite_sequence') ORDER BY name")]
        return {n: sorted(conn.execute(f"SELECT * FROM {n}").fetchall(), key=repr) for n in names}
    finally:
        conn.close()


# ----------------------------------------------------------------- new stores


def test_a_new_store_has_the_faulty_fts_option_off_and_core_secure_delete_on(tmp_path: Path) -> None:
    store = TranscriptStore(tmp_path / "meetings.db")
    try:
        assert not fts_secure_delete_enabled(store._conn)
        assert store._conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
        assert not store.maintenance_pending()
    finally:
        store.close()


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_deleting_a_realistic_meeting_keeps_the_index_whole_and_scrubs_the_text(tmp_path: Path, seed: int) -> None:
    rng = random.Random(seed)
    store = TranscriptStore(tmp_path / "meetings.db")
    try:
        add_meeting(store, "m0", 120, rng)
        add_meeting(store, "m1", 120, rng, canary=True)
        kept = add_meeting(store, "m2", 120, rng)
        for i in range(10):
            store.correct_segment(kept[i], sentence(rng))
        assert CANARY.encode() in files(tmp_path)
        store.delete_meeting("m1")
        assert fts_ok(store) and not store.maintenance_pending()
        assert store.search(CANARY) == [] and store.meeting("m1") is None
        assert CANARY.encode() not in files(tmp_path)  # database and WAL, while still open
        assert len(store.segments("m0")) == len(store.segments("m2")) == 120
        keeps_saving(store, rng)
        assert fts_ok(store)
    finally:
        store.close()
    assert CANARY.encode() not in files(tmp_path)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_many_corrections_keep_the_index_whole_and_a_new_meeting_starts(tmp_path: Path, seed: int) -> None:
    rng = random.Random(seed)
    store = TranscriptStore(tmp_path / "meetings.db")
    try:
        ids = add_meeting(store, "m0", 200, rng)
        for i in range(60):
            store.correct_segment(ids[rng.randrange(len(ids))], sentence(rng))  # none refused
        assert fts_ok(store)
        keeps_saving(store, rng)
    finally:
        store.close()
    reopened = TranscriptStore(tmp_path / "meetings.db")
    try:
        assert fts_ok(reopened)
    finally:
        reopened.close()


def test_retention_purge_keeps_the_index_whole_and_scrubs_the_text(tmp_path: Path) -> None:
    rng = random.Random(7)
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = TranscriptStore(tmp_path / "meetings.db")
    try:
        for i in range(3):
            add_meeting(store, f"old-{i}", 80, rng, canary=True, ended=old + timedelta(days=i))
        add_meeting(store, "recent", 80, rng, ended=datetime(2026, 11, 1, tzinfo=timezone.utc))
        deleted = store.purge_expired("2026-06-01T00:00:00+00:00")
        assert sorted(deleted) == ["old-0", "old-1", "old-2"]
        assert fts_ok(store) and not store.maintenance_pending()
        assert CANARY.encode() not in files(tmp_path)
        assert len(store.segments("recent")) == 80
        keeps_saving(store, rng)
    finally:
        store.close()


def test_redaction_keeps_the_index_whole_and_scrubs_the_text(tmp_path: Path) -> None:
    rng = random.Random(11)
    store = TranscriptStore(tmp_path / "meetings.db")
    try:
        ids = add_meeting(store, "m0", 200, rng, canary=True)
        for segment in ids[::10]:  # the 20 lines that carry the canary
            store.redact_segment(segment)
        assert fts_ok(store) and not store.maintenance_pending()
        assert store.search(CANARY) == [] and CANARY.encode() not in files(tmp_path)
        assert len(store.segments("m0")) == 180
        keeps_saving(store, rng)
    finally:
        store.close()


# ----------------------------------------------------------- existing stores


def damaged_legacy_store(path: Path) -> tuple[bool, dict[str, list[tuple]]]:
    """A correction-heavy fixture on a pre-fix store, with minutes saved, written by
    hand with FTS5 'secure-delete' on (as the old schema did). Returns whether the
    index ended up damaged and a dump of every saved row."""
    make_legacy(path)
    store = TranscriptStore.__new__(TranscriptStore)  # the old behaviour: no repair at open
    store.path = str(path)
    import threading
    store._lock = threading.RLock()
    store._conn = connect(str(path))
    rng = random.Random(0)
    sentences = ("the project is going well and we will meet tomorrow",
                 "the project is still running and this is the action item",
                 "we discussed the budget and the action items", "مراجعة مشروع اجتماع محضر ميزانية")
    target = []
    try:
        for group in range(3):
            meeting_id = f"m{group}"
            store.create_meeting(meeting_id, title=f"study meeting {group}", timezone_name="UTC")
            store.save_epoch(meeting_id, source="system", epoch=1, device_id="fake", device_label="synthetic",
                             native_rate=48_000, native_channels=1, start_ms=0, end_ms=None)
            for index in range(10):
                segment = store.add_final(meeting_id, source="system", epoch=1, start_ms=index * 1_000,
                                          end_ms=index * 1_000 + 950,
                                          text=f"{sentences[rng.randrange(4)]} entry {index} for meeting {group}",
                                          language="en", engine_id="fake", model_hash="0" * 64)
                if group == 1:
                    target.append(segment)
            store.set_status(meeting_id, "stopped", ended=True)
        refs = [(s.id, s.revision) for s in store.segments("m0")]
        store.save_minutes_version(
            "m0", transcript_revision=store.meeting("m0").transcript_revision, snapshot_hash="h" * 64,
            snapshot_refs=refs, redactions_at_snapshot=0, provider="fake", model_id="fake", prompt_version="1",
            schema_version=1, output_language="en", input_tokens=10, output_tokens=5, requests=1, parts=1,
            incomplete=False, warnings=[], items=[{"kind": "summary", "text": "synthetic summary",
                                                    "evidence": [list(refs[0])]}])
        for index in range(60):
            store.correct_segment(target[index % len(target)],
                                  f"corrected budget review for the meeting and future action plan {index}")
        damaged = not fts_ok(store)
    finally:
        store._conn.close()
    return damaged, table_dump(path)


def test_a_damaged_pre_hotfix_store_is_repaired_in_place_without_losing_rows(tmp_path: Path) -> None:
    path = tmp_path / "meetings.db"
    damaged, before = damaged_legacy_store(path)
    if sqlite3.sqlite_version == "3.42.0":
        assert damaged, "the fixture no longer reproduces the damage it guards against"
    statements: list[str] = []
    real_connect = store_module.connect

    def traced(p: str) -> sqlite3.Connection:
        conn = real_connect(p)
        conn.set_trace_callback(statements.append)
        return conn

    store_module.connect = traced
    try:
        store = TranscriptStore(path)
    finally:
        store_module.connect = real_connect
    try:
        assert any("'rebuild'" in s for s in statements)
        assert not fts_secure_delete_enabled(store._conn)
        assert store._conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
        assert fts_ok(store) and not store.maintenance_pending()
        assert table_dump(path) == before  # every meeting, revision, event and minutes row kept
        assert len(store.search("future action plan")) == 10
        assert len(store.minutes_versions("m0")) == 1
        keeps_saving(store, random.Random(5))
        assert fts_ok(store)
    finally:
        store.close()

    statements.clear()
    store_module.connect = traced
    try:
        again = TranscriptStore(path)  # opening again does not rebuild again
    finally:
        store_module.connect = real_connect
    try:
        assert not any("'rebuild'" in s for s in statements) and fts_ok(again)
    finally:
        again.close()


def test_an_undamaged_pre_hotfix_store_is_switched_over_and_keeps_working(tmp_path: Path) -> None:
    path = tmp_path / "meetings.db"
    make_legacy(path)
    store = TranscriptStore(path)
    try:
        assert not fts_secure_delete_enabled(store._conn) and fts_ok(store)
        rng = random.Random(3)
        add_meeting(store, "m0", 120, rng, canary=True)
        add_meeting(store, "m1", 120, rng)
        store.delete_meeting("m0")
        assert fts_ok(store) and CANARY.encode() not in files(tmp_path)
    finally:
        store.close()


def test_a_failed_repair_rolls_back_and_never_resets_the_database(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "meetings.db"
    damaged, before = damaged_legacy_store(path)
    real_connect = store_module.connect

    def refusing(p: str) -> sqlite3.Connection:
        conn = real_connect(p)

        def authorizer(action, table, *_):
            if action == sqlite3.SQLITE_INSERT and table == "search_fts" and _[-1] is None:
                return sqlite3.SQLITE_DENY  # the rebuild statement, not the index triggers
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorizer)
        return conn

    monkeypatch.setattr(store_module, "connect", refusing)
    with pytest.raises(StorageError):
        TranscriptStore(path)
    monkeypatch.undo()
    raw = sqlite3.connect(path)
    try:
        assert raw.execute("SELECT v FROM search_fts_config WHERE k = 'secure-delete'").fetchone()[0] == 1
        assert raw.execute("SELECT COUNT(*) FROM settings WHERE key = 'maintenance_pending'").fetchone()[0] == 0
    finally:
        raw.close()
    assert table_dump(path) == before  # rolled back, nothing reset or deleted
    store = TranscriptStore(path)  # the next normal open repairs it
    try:
        assert not fts_secure_delete_enabled(store._conn) and fts_ok(store)
        assert table_dump(path) == before
    finally:
        store.close()


# ------------------------------------------------------- durable tidy-up


def test_a_stop_right_after_a_committed_delete_is_finished_at_the_next_start(tmp_path: Path) -> None:
    """A real child process deletes a meeting and exits just before the index rebuild:
    the deletion is committed and the text is still in the files. The marker was
    committed with the deletion, so the next start knows the tidy-up is owed, and
    finishing it removes the text and leaves a whole index."""
    rng = random.Random(9)
    path = tmp_path / "meetings.db"
    store = TranscriptStore(path)
    add_meeting(store, "m0", 120, rng, canary=True)
    add_meeting(store, "m1", 120, rng)
    store.close()
    code = (
        "import os, sys\n"
        "from meeting_minutes.storage.store import TranscriptStore\n"
        "s = TranscriptStore(sys.argv[1])\n"
        "def stop(sql):\n"
        "    if \"VALUES('rebuild')\" in sql:\n"
        "        os._exit(37)\n"
        "s._conn.set_trace_callback(stop)\n"
        "s.delete_meeting('m0')\n"
        "raise SystemExit(99)\n"
    )
    child = subprocess.run([sys.executable, "-c", code, str(path)], env={**os.environ, "PYTHONPATH": str(SRC)},
                           capture_output=True, text=True, timeout=60)
    assert child.returncode == 37, child.stderr
    assert CANARY.encode() in files(tmp_path)  # without the tidy-up the text is still on disk

    store = TranscriptStore(path)
    try:
        assert store.meeting("m0") is None and store.search(CANARY) == []  # the deletion was committed
        assert store.maintenance_pending()  # the window's start-up retry (run_retention) sees this
        store.scrub()
        assert not store.maintenance_pending() and fts_ok(store)
        assert CANARY.encode() not in files(tmp_path)
        keeps_saving(store, rng)
    finally:
        store.close()


def test_a_failed_rebuild_still_folds_the_wal_back_but_stays_pending(tmp_path: Path) -> None:
    rng = random.Random(13)
    path = tmp_path / "meetings.db"
    store = TranscriptStore(path)
    try:
        add_meeting(store, "m0", 120, rng, canary=True)
        add_meeting(store, "m1", 120, rng)
        store.scrub()

        def refuse_rebuild(action, table, column, database, trigger):
            if action == sqlite3.SQLITE_INSERT and table == "search_fts" and trigger is None:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        store._conn.set_authorizer(refuse_rebuild)
        with pytest.raises(MaintenanceError) as caught:
            store.delete_meeting("m0")
        assert caught.value.deleted == ("m0",)
        assert store.meeting("m0") is None and store.search(CANARY) == []
        assert store.maintenance_pending()  # a clean WAL alone does not prove the index was scrubbed
        assert (tmp_path / "meetings.db-wal").stat().st_size == 0  # the checkpoint was still attempted
        store._conn.set_authorizer(None)
        store.scrub()
        assert not store.maintenance_pending() and fts_ok(store)
        assert CANARY.encode() not in files(tmp_path)
    finally:
        store.close()


def test_a_busy_reader_keeps_the_tidy_up_pending_until_it_leaves(tmp_path: Path) -> None:
    rng = random.Random(17)
    path = tmp_path / "meetings.db"
    store = TranscriptStore(path)
    reader = sqlite3.connect(path)
    try:
        add_meeting(store, "m0", 120, rng, canary=True)
        add_meeting(store, "m1", 120, rng)
        store.scrub()
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM segments").fetchall()
        with pytest.raises(MaintenanceError, match="busy"):
            store.delete_meeting("m0")
        assert store.maintenance_pending() and fts_ok(store)
        keeps_saving(store, rng, lines=50)  # saving continues while the tidy-up is owed
        reader.rollback()
        store.scrub()
        assert not store.maintenance_pending() and fts_ok(store)
        assert CANARY.encode() not in files(tmp_path)
    finally:
        reader.close()
        store.close()


# ------------------------------------------- several connections


def test_a_marker_from_an_earlier_build_is_still_retried_and_cleared(tmp_path: Path) -> None:
    store = TranscriptStore(tmp_path / "meetings.db")
    try:
        store.set_setting("maintenance_pending", True)  # the value earlier builds wrote
        assert store.maintenance_pending()
        store.scrub()
        assert not store.maintenance_pending()
    finally:
        store.close()


def test_a_failed_tidy_up_keeps_the_obligation_it_did_not_finish(tmp_path: Path) -> None:
    """The marker left by a failed rebuild is the same one the deletion wrote, so a
    retry by any connection that reads it later covers that deletion."""
    rng = random.Random(19)
    store = TranscriptStore(tmp_path / "meetings.db")
    try:
        add_meeting(store, "m0", 40, rng, canary=True)
        store._conn.set_authorizer(lambda action, table, column, database, trigger: sqlite3.SQLITE_DENY
                                   if action == sqlite3.SQLITE_INSERT and table == "search_fts" and trigger is None
                                   else sqlite3.SQLITE_OK)
        with pytest.raises(MaintenanceError):
            store.delete_meeting("m0")
        owed = store._read("SELECT value_json FROM settings WHERE key = 'maintenance_pending'")
        with pytest.raises(MaintenanceError):
            store.scrub()
        assert store._read("SELECT value_json FROM settings WHERE key = 'maintenance_pending'") == owed
        store._conn.set_authorizer(None)
        other = TranscriptStore(tmp_path / "meetings.db")  # e.g. the next start, or another window
        try:
            other.scrub()
            assert not other.maintenance_pending() and fts_ok(other)
        finally:
            other.close()
        assert CANARY.encode() not in files(tmp_path)
    finally:
        store.close()
