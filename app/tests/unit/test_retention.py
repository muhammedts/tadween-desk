"""History retention: the policy setting and the store's expiry/purge rules,
on real SQLite files in a temporary folder (never the user's History)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import sqlite3

import pytest

from meeting_minutes import retention
from meeting_minutes.storage.store import MaintenanceError, StorageError, TranscriptStore

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds")


def seed(store: TranscriptStore, meeting_id: str, *, ended: datetime | None, started: datetime | None = None,
         status: str = "stopped", text: str = "budget review") -> None:
    start = started or (ended - timedelta(hours=1) if ended else NOW)
    store.create_meeting(meeting_id, title=f"title {meeting_id}", timezone_name="UTC", started_at=iso(start))
    store.save_epoch(meeting_id, source="system", epoch=1, device_id="d", device_label="d", native_rate=48_000,
                     native_channels=1, start_ms=0, end_ms=None)
    store.add_final(meeting_id, source="system", epoch=1, start_ms=0, end_ms=900, text=text, language=None,
                    engine_id="e", model_hash="h")
    with store.held():
        store._conn.execute("UPDATE meetings SET status = ?, ended_at = ? WHERE id = ?",
                            (status, iso(ended) if ended else None, meeting_id))


@pytest.mark.parametrize("value", [None, {}, {"days": 7}, {"days": True}, {"days": "90"}, [90], "90", {"day": 90},
                                   {"days": []}, {"days": {}}, {"days": [90]}, {"days": 90.0}, {"days": False}])
def test_a_missing_or_unknown_setting_never_counts_as_a_choice(value) -> None:
    assert retention.parse(value) is retention.NOT_CHOSEN


def test_valid_choices_round_trip() -> None:
    for days in (None, 30, 90, 365):
        assert retention.parse(retention.to_setting(days, NOW)) == days
    with pytest.raises(ValueError):
        retention.to_setting(7, NOW)
    assert retention.cutoff(90, NOW) == "2026-08-01T12:00:00.000+00:00"


def test_expiry_boundary_statuses_exclusions_and_bad_times(tmp_path: Path) -> None:
    store = TranscriptStore(tmp_path / "m.db")
    cutoff = NOW - timedelta(days=30)
    seed(store, "old", ended=cutoff - timedelta(milliseconds=1))
    seed(store, "at-cutoff", ended=cutoff)  # exactly at the boundary: kept
    seed(store, "recent", ended=NOW - timedelta(days=1))
    seed(store, "future", ended=NOW + timedelta(days=400))  # clock moved back: kept
    seed(store, "interrupted-no-end", ended=None, started=cutoff - timedelta(days=5), status="interrupted")
    for status in ("starting", "capturing", "paused", "degraded", "stopping"):
        seed(store, f"running-{status}", ended=None, started=cutoff - timedelta(days=50), status=status)
    seed(store, "active", ended=cutoff - timedelta(days=9))
    seed(store, "bad-time", ended=None, started=cutoff - timedelta(days=9))
    with store.held():
        store._conn.execute("UPDATE meetings SET started_at = 'not a time' WHERE id = 'bad-time'")

    expired = store.expired_meetings(retention.cutoff(30, NOW), exclude={"active"})
    assert expired == ["interrupted-no-end", "old"]  # oldest first; only finished ones
    assert store.purge_expired(retention.cutoff(30, NOW), exclude={"active"}) == ["interrupted-no-end", "old"]
    left = {m.id for m in store.list_meetings(limit=100)}
    assert "old" not in left and "interrupted-no-end" not in left
    assert {"at-cutoff", "recent", "future", "active", "bad-time"} <= left
    assert all(f"running-{s}" in left for s in ("starting", "capturing", "paused", "degraded", "stopping"))
    # Their text, revisions and search entries went with them.
    with store.held():
        rows = store._conn.execute("SELECT COUNT(*) FROM segment_revisions WHERE meeting_id IN ('old',"
                                   " 'interrupted-no-end')").fetchone()[0]
        docs = store._conn.execute("SELECT COUNT(*) FROM search_documents WHERE meeting_id = 'old'").fetchone()[0]
    assert rows == 0 and docs == 0
    assert {h.meeting_id for h in store.search("budget", limit=100)} == left


def test_purge_is_bounded_and_all_or_nothing(tmp_path: Path, monkeypatch) -> None:
    store = TranscriptStore(tmp_path / "m.db")
    for i in range(5):
        seed(store, f"old-{i}", ended=NOW - timedelta(days=100 + i))
    before = retention.cutoff(90, NOW)
    assert store.purge_expired(before, limit=2) == ["old-4", "old-3"]

    calls = []
    real = TranscriptStore._delete_rows

    def fail_second(c, meeting_id):
        calls.append(meeting_id)
        if len(calls) == 2:
            raise StorageError("disk_full")
        real(c, meeting_id)

    monkeypatch.setattr(TranscriptStore, "_delete_rows", staticmethod(fail_second))
    with pytest.raises(StorageError):
        store.purge_expired(before, limit=3)
    monkeypatch.undo()
    # The first deletion in the failed batch was rolled back with the rest.
    assert sorted(m.id for m in store.list_meetings()) == ["old-0", "old-1", "old-2"]
    assert len(store.search("budget")) == 3


def test_the_choice_and_shortcut_persist_across_restart(tmp_path: Path) -> None:
    store = TranscriptStore(tmp_path / "m.db")
    assert store.get_setting(retention.SETTING_KEY) is None
    store.set_setting(retention.SETTING_KEY, retention.to_setting(365, NOW))
    store.set_setting(retention.SETTING_KEY, retention.to_setting(None, NOW))
    store.set_setting("mic_shortcut", "Ctrl+Alt+K")
    store.close()
    reopened = TranscriptStore(tmp_path / "m.db")
    assert retention.parse(reopened.get_setting(retention.SETTING_KEY)) is None  # keep until deleted
    assert reopened.get_setting("mic_shortcut") == "Ctrl+Alt+K"


def refuse_index_maintenance(store: TranscriptStore) -> list[str]:
    """A real SQLite refusal of the FTS merge only, after the deleting transaction;
    the deletion statements and their index triggers still run."""
    denied: list[str] = []

    def authorizer(action, table, column, database, trigger):
        if action == sqlite3.SQLITE_INSERT and table == "search_fts" and trigger is None:
            denied.append(table)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    store._conn.set_authorizer(authorizer)
    return denied


def test_a_maintenance_failure_after_commit_still_reports_what_was_deleted(tmp_path: Path) -> None:
    store = TranscriptStore(tmp_path / "m.db")
    for i in range(3):
        seed(store, f"old-{i}", ended=NOW - timedelta(days=100 + i), text=f"pineapple {i}")
    segment = store.snapshot("old-0").segments[0].id
    denied = refuse_index_maintenance(store)

    with pytest.raises(MaintenanceError) as caught:
        store.purge_expired(retention.cutoff(90, NOW), limit=2)
    assert denied and caught.value.deleted == ("old-2", "old-1")
    assert isinstance(caught.value, StorageError)  # still a failure for callers that only know StorageError
    assert sorted(m.id for m in store.list_meetings()) == ["old-0"]  # committed: really gone
    assert [hit.meeting_id for hit in store.search("pineapple")] == ["old-0"]

    # The shared deletion paths report the same way: committed, maintenance failed.
    with pytest.raises(MaintenanceError) as caught:
        store.redact_segment(segment)
    assert caught.value.deleted == ()
    assert store.search("pineapple") == []
    with pytest.raises(MaintenanceError) as caught:
        store.delete_meeting("old-0")
    assert caught.value.deleted == ("old-0",) and store.list_meetings() == []

    store._conn.set_authorizer(None)
    store.scrub()  # the retry succeeds once maintenance is possible again
    store.close()


def test_a_failure_before_commit_is_not_reported_as_maintenance(tmp_path: Path, monkeypatch) -> None:
    store = TranscriptStore(tmp_path / "m.db")
    seed(store, "old", ended=NOW - timedelta(days=100))

    def fail(c, meeting_id):
        raise StorageError("disk_full")

    monkeypatch.setattr(TranscriptStore, "_delete_rows", staticmethod(fail))
    with pytest.raises(StorageError) as caught:
        store.purge_expired(retention.cutoff(90, NOW))
    assert not isinstance(caught.value, MaintenanceError)
    monkeypatch.undo()
    assert [m.id for m in store.list_meetings()] == ["old"]
    store.close()
