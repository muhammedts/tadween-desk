"""Transcript workflow in the real window, offscreen: retention choice and
cleanup, meeting controls on every page, keyboard shortcuts, paragraph direction and
copy, correction failures, long and empty transcripts. Real PySide6 widgets, real
SQLite files in a temporary folder and real export files; devices, recognizer and the
Save As chooser are the same stand-ins as in test_desktop_app.

Not covered: a person using the native chooser, a screen reader,
real 200% Windows scaling and real devices/models."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sqlite3  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QKeySequence  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox, QStyleOptionViewItem  # noqa: E402
from test_desktop_app import FILE_FILTERS, Chooser, lines, say, wait_until, window_for  # noqa: E402

from meeting_minutes import retention  # noqa: E402
from meeting_minutes.domain.contracts import Source  # noqa: E402
from meeting_minutes.storage.store import StorageError, TranscriptStore  # noqa: E402
from meeting_minutes.ui.desktop import LRM, RTL_ROLE, TEXT_ROLE, MainWindow  # noqa: E402

NOW = datetime.now(timezone.utc)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds")


def seed(path: Path, meeting_id: str, *, ended: datetime, texts=("budget review",), status: str = "stopped",
         title: str | None = None, step_ms: int = 1000) -> None:
    store = TranscriptStore(path)
    store.create_meeting(meeting_id, title=title or f"Meeting {meeting_id}", timezone_name="UTC",
                         started_at=iso(ended - timedelta(hours=2)))
    for source in ("mic", "system"):
        store.save_epoch(meeting_id, source=source, epoch=1, device_id="d", device_label="d", native_rate=48_000,
                         native_channels=1, start_ms=0, end_ms=None)
    with store.held():
        for i, text in enumerate(texts):
            store.add_final(meeting_id, source="system" if i % 2 else "mic", epoch=1, start_ms=i * step_ms,
                            end_ms=i * step_ms + step_ms - 10, text=text, language=None, engine_id="e",
                            model_hash="h")
        store._conn.execute("UPDATE meetings SET status = ?, ended_at = ? WHERE id = ?",
                            (status, iso(ended), meeting_id))
    store.close()


def ids(store: TranscriptStore) -> set[str]:
    return {m.id for m in store.list_meetings(limit=100)}


def press(window: MainWindow, keys: str) -> None:
    sequence = QKeySequence(keys)[0]
    QTest.keyClick(window, sequence.key(), sequence.keyboardModifiers())
    QApplication.processEvents()


def test_nothing_expires_before_a_choice_then_the_confirmed_choice_applies_and_persists(
        app, tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "meetings.db"
    seed(db, "old", ended=NOW - timedelta(days=200))
    seed(db, "recent", ended=NOW - timedelta(days=2))
    window, store = window_for(tmp_path)
    # First run: the policy is shown, 90 days proposed, and startup cleanup deleted nothing.
    assert window.retention_prompt.isVisibleTo(window)
    assert "Not chosen yet" in window.retention_current.text()
    assert window.retention_combo.currentText() == "Delete after 90 days (proposed)"
    assert ids(store) == {"old", "recent"} and window.run_retention() == []

    asked: list[str] = []

    def answer(reply):
        def question(parent, title, text, *a, **k):
            asked.append(text)
            return reply
        return question

    monkeypatch.setattr(QMessageBox, "question", answer(QMessageBox.StandardButton.No))
    window.apply_retention()
    assert "1 saved meeting(s) ended more than 90 days ago" in asked[-1] and "Save As" in asked[-1]
    assert ids(store) == {"old", "recent"} and store.get_setting(retention.SETTING_KEY) is None
    assert window.retention_prompt.isVisibleTo(window)

    monkeypatch.setattr(QMessageBox, "question", answer(QMessageBox.StandardButton.Yes))
    window.apply_retention()
    assert ids(store) == {"recent"}
    assert not window.retention_prompt.isVisibleTo(window)
    assert "Delete after 90 days" in window.retention_current.text()
    window.close()
    store.close()

    # Restart: the choice is kept, no prompt; "keep until I delete" never deletes.
    seed(db, "older", ended=NOW - timedelta(days=500))
    window, store = window_for(tmp_path)
    assert not window.retention_prompt.isVisibleTo(window) and window.retention_combo.currentIndex() == 2
    assert ids(store) == {"recent"}  # the 500-day-old meeting expired at startup
    seed(db, "oldest", ended=NOW - timedelta(days=900))
    window.retention_combo.setCurrentIndex(0)
    asked.clear()
    window.apply_retention()
    assert not asked and window.run_retention() == [] and ids(store) == {"recent", "oldest"}
    window.close()


def test_cleanup_skips_the_current_meeting_reports_failure_and_clears_stale_views(
        app, tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "meetings.db"
    seed(db, "old-a", ended=NOW - timedelta(days=40), texts=("pineapple budget",))
    seed(db, "old-b", ended=NOW - timedelta(days=45), texts=("pineapple plan",))
    store = TranscriptStore(db)
    store.set_setting(retention.SETTING_KEY, retention.to_setting(30, NOW))
    store.close()
    clock = [NOW - timedelta(days=60)]  # nothing has expired yet
    window, store = window_for(tmp_path, clock=lambda: clock[0])
    assert ids(store) == {"old-a", "old-b"}

    # A meeting that was just recorded and stopped is still the current one.
    window.start_button.click()
    say(window, Source.SYSTEM, [(0.3, "hello")])
    wait_until(lambda: any(line.endswith("\nhello") for line in lines(window.live_list)))
    window.stop_meeting()
    wait_until(lambda: window.pages.currentWidget() is window.saved_page and window.saved_list.count() == 1)
    current = window.session.meeting_id

    window.open_meeting("old-a")
    window.search_edit.setText("pineapple")
    window.run_search()
    assert len(lines(window.search_results)) == 2
    window.open_meeting("old-a")
    clock[0] = NOW + timedelta(days=400)  # now even the current meeting is old enough

    real = store.purge_expired

    def broken(*a, **k):
        raise StorageError("io_error")

    monkeypatch.setattr(store, "purge_expired", broken)
    assert window.run_retention() == []
    assert "could not run (io_error)" in window.retention_status.text()
    assert ids(store) == {"old-a", "old-b", current}

    monkeypatch.setattr(store, "purge_expired", real)
    assert sorted(window.run_retention()) == ["old-a", "old-b"]
    assert ids(store) == {current}
    # The open view of a deleted meeting and the visible search results are gone.
    assert window.pages.currentWidget() is window.history_page and window._open_meeting is None
    assert window.saved_list.count() == 0
    assert lines(window.search_results) == ["No matches in current transcript text or titles."]
    window.close()


def refuse_index_maintenance(store: TranscriptStore) -> list[str]:
    """A real SQLite refusal of the FTS merge only (as in test_retention); deletions
    and their index triggers still run and commit."""
    denied: list[str] = []

    def authorizer(action, table, column, database, trigger):
        if action == sqlite3.SQLITE_INSERT and table == "search_fts" and trigger is None:
            denied.append(table)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    store._conn.set_authorizer(authorizer)
    return denied


def test_committed_deletions_clear_views_when_index_maintenance_fails(app, tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "meetings.db"
    seed(db, "old", ended=NOW - timedelta(days=40), texts=("pineapple budget",))
    seed(db, "kept", ended=NOW - timedelta(days=2), texts=("pineapple plan", "mango notes"))
    store = TranscriptStore(db)
    store.set_setting(retention.SETTING_KEY, retention.to_setting(30, NOW))
    store.close()
    clock = [NOW - timedelta(days=60)]  # nothing has expired yet
    window, store = window_for(tmp_path, clock=lambda: clock[0])
    warnings: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text, *a: warnings.append(text))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    window.search_edit.setText("pineapple")
    window.run_search()
    window.open_meeting("old")
    assert len(lines(window.search_results)) == 2

    # Hourly cleanup: the deletion commits, then the index merge is refused.
    denied = refuse_index_maintenance(store)
    clock[0] = NOW
    assert window.run_retention() == ["old"]
    assert denied and ids(store) == {"kept"}
    status = window.retention_status.text()
    assert "deleted 1 meeting(s)" in status and "tidy-up" in status and "deleted nothing" not in status
    assert window._open_meeting is None and window.saved_list.count() == 0
    assert all("budget" not in line for line in lines(window.search_results))

    # A manual redaction and deletion are reported as done, with the maintenance warning.
    window.open_meeting("kept")
    window.saved_list.setCurrentRow(0)
    window.redact_selected()
    assert all("pineapple plan" not in line for line in lines(window.saved_list))
    assert "was removed" in warnings[-1] and "Nothing was changed" not in warnings[-1]
    assert window.delete_meeting("kept") is True
    assert ids(store) == set() and window._open_meeting is None and window.saved_list.count() == 0
    assert "was removed" in warnings[-1]

    # The next cleanup run retries the maintenance once it is possible again.
    store._conn.set_authorizer(None)
    assert window._maintenance_pending
    window.run_retention()
    assert not window._maintenance_pending
    window.close()


def test_a_tidy_up_left_pending_before_a_restart_is_retried_at_startup(app, tmp_path: Path) -> None:
    from meeting_minutes.storage.store import MAINTENANCE_KEY

    db = tmp_path / "meetings.db"
    seed(db, "kept", ended=NOW - timedelta(days=2))
    store = TranscriptStore(db)
    store.set_setting(MAINTENANCE_KEY, True)  # an earlier run's tidy-up did not finish
    store.close()
    window, store = window_for(tmp_path)  # no retention choice: the retry still runs
    assert window._retention is retention.NOT_CHOSEN
    assert not store.maintenance_pending() and not window._maintenance_pending
    assert ids(store) == {"kept"}
    window.close()


@pytest.mark.parametrize("bad", [{"days": []}, {"days": {}}, {"days": 90.0}])
def test_an_unreadable_retention_setting_opens_as_not_chosen(app, tmp_path: Path, bad) -> None:
    db = tmp_path / "meetings.db"
    seed(db, "old", ended=NOW - timedelta(days=900))
    store = TranscriptStore(db)
    store.set_setting(retention.SETTING_KEY, bad)
    store.close()
    window, store = window_for(tmp_path)
    assert window._retention is retention.NOT_CHOSEN and window.retention_prompt.isVisibleTo(window)
    assert "Not chosen yet" in window.retention_current.text()
    assert window.run_retention() == [] and ids(store) == {"old"}
    window.close()


def test_meeting_controls_stay_on_every_page_and_work_from_the_keyboard(app, tmp_path: Path) -> None:
    window, store = window_for(tmp_path)
    window.activateWindow()
    QTest.qWaitForWindowActive(window)
    assert not window.meeting_bar.isVisible()
    window.start_button.click()
    assert window.meeting_bar.isVisible()

    press(window, "Ctrl+3")
    assert window.pages.currentWidget() is window.history_page and window.meeting_bar.isVisible()
    window.mic_button.click()  # mute from the History page
    assert not window.session.mic_included
    assert "excluded" in window.mic_banner.text() and window.mic_button.text().startswith("Unmute mic")
    assert "excluded" in window.mic_button.accessibleDescription()

    press(window, "F1")
    assert window.pages.currentWidget() is window.settings_page and window.meeting_bar.isVisible()
    press(window, "Ctrl+P")
    assert window.session.state.value == "paused" and window.pause_button.text() == "Resume"
    press(window, "Ctrl+P")
    assert window.session.state.value in ("capturing", "degraded")
    assert not window.session.mic_included  # Resume keeps the mic excluded
    press(window, "Ctrl+2")
    assert window.pages.currentWidget() is window.live_page
    press(window, "Ctrl+Shift+S")
    wait_until(lambda: window.pages.currentWidget() is window.saved_page)
    assert not window.meeting_bar.isVisible()
    assert store.meeting(window.session.meeting_id).status == "stopped"
    window.close()


def test_the_mute_shortcut_can_be_changed_but_not_to_a_plain_or_taken_key(app, tmp_path: Path) -> None:
    window, store = window_for(tmp_path)
    for keys, problem in (("M", "Ctrl or Alt"), ("Ctrl+F", "already used"), ("Ctrl+C", "already used")):
        window.mic_keys_edit.setKeySequence(QKeySequence(keys))
        window.save_mic_shortcut()
        assert problem in window.shortcut_status.text()
    assert store.get_setting("mic_shortcut") is None
    window.mic_keys_edit.setKeySequence(QKeySequence("Ctrl+Alt+K"))
    window.save_mic_shortcut()
    assert window.mic_shortcut.key() == QKeySequence("Ctrl+Alt+K")
    window.close()
    store.close()

    window, store = window_for(tmp_path)  # restart: the saved shortcut is used
    assert window.mic_shortcut.key() == QKeySequence("Ctrl+Alt+K")
    window.start_button.click()
    QTest.keyClick(window, Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)
    assert not window.session.mic_included and "Ctrl+Alt+K" in window.mic_button.text()
    window.stop_meeting()
    wait_until(lambda: window.pages.currentWidget() is window.saved_page)
    window.close()


ARABIC = "الميزانية تمت الموافقة عليها"
MIXED_AR = "الموعد النهائي Thursday الخميس"
MIXED_EN = "deadline الخميس Thursday"


def test_lines_take_their_own_direction_copy_is_plain_and_arabic_search_jumps(app, tmp_path: Path,
                                                                             monkeypatch) -> None:
    seed(tmp_path / "meetings.db", "m", ended=NOW, texts=("budget review", ARABIC, MIXED_AR, MIXED_EN),
         title="اجتماع / review")
    window, store = window_for(tmp_path)
    window.open_meeting("m")
    expected = [("budget review", False), (ARABIC, True), (MIXED_AR, True), (MIXED_EN, False)]
    delegate = window.saved_list.itemDelegate()
    for row, (text, rtl) in enumerate(expected):
        item = window.saved_list.item(row)
        assert item.data(TEXT_ROLE) == text and bool(item.data(RTL_ROLE)) is rtl
        assert LRM not in item.text() and item.text().endswith("\n" + text)
        option = QStyleOptionViewItem()
        delegate.initStyleOption(option, window.saved_list.model().index(row, 0))
        assert option.direction == (Qt.LayoutDirection.RightToLeft if rtl else Qt.LayoutDirection.LeftToRight)
        assert (LRM in option.text) is rtl  # the time/source label is kept left-to-right
    assert "saved revision 4" in window.saved_title.text()

    window.saved_list.item(1).setSelected(True)
    window.saved_list.item(2).setSelected(True)
    window.copy_selected()
    assert QGuiApplication.clipboard().text() == f"{ARABIC}\n{MIXED_AR}"

    window.search_edit.setText("الخميس")
    window.run_search()
    hits = lines(window.search_results)
    assert len(hits) == 2 and all("الخميس" in h for h in hits)
    window._open_search_hit(window.search_results.item(0))
    assert window.saved_list.currentItem().data(TEXT_ROLE) in (MIXED_AR, MIXED_EN)
    assert window.saved_list.currentItem().text().startswith(("[00:02] Mic", "[00:03] System"))

    # Cancelling a correction changes nothing; a failed save keeps the previous revision.
    window.saved_list.setCurrentRow(1)
    segment = window._selected_segment()
    monkeypatch.setattr(QInputDialog, "getMultiLineText", lambda *a, **k: ("changed", False))
    window.correct_selected()
    monkeypatch.setattr(QInputDialog, "getMultiLineText", lambda *a, **k: ("الميزانية النهائية", True))
    monkeypatch.setattr(store, "correct_segment", lambda *a: (_ for _ in ()).throw(StorageError("disk_full")))
    warnings: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text, *a: warnings.append(text))
    window.correct_selected()
    assert warnings and "disk_full" in warnings[0] and "Nothing was changed" in warnings[0]
    assert [r.text for r in store.revisions(segment)] == [ARABIC]
    assert store.meeting("m").transcript_revision == 4
    assert window.saved_list.item(1).data(TEXT_ROLE) == ARABIC
    monkeypatch.undo()

    # A real correction shows its version and is what search finds.
    monkeypatch.setattr(QInputDialog, "getMultiLineText", lambda *a, **k: ("الميزانية النهائية", True))
    window.saved_list.setCurrentRow(1)
    window.correct_selected()
    assert "corrected (version 2" in window.saved_list.currentItem().text()
    assert "saved revision 5" in window.saved_title.text()
    assert [r.origin for r in store.revisions(segment)] == ["asr", "user"]
    assert store.search("الموافقة") == [] and len(store.search("النهائية")) == 1
    window.close()


def test_a_long_two_hour_transcript_opens_searches_and_exports_in_order(app, tmp_path: Path) -> None:
    count = 4000
    step = 7_200_000 // count  # 1.8 s apart: first at 0:00, last at 1:59:58
    texts = [f"item{i} budget review" if i % 2 else f"البند {i} item{i} الميزانية" for i in range(count)]
    seed(tmp_path / "meetings.db", "long", ended=NOW, texts=texts, step_ms=step)
    target = tmp_path / "out" / "long.txt"
    target.parent.mkdir()
    window, store = window_for(tmp_path, chooser=Chooser((str(target), FILE_FILTERS[0])))

    started = time.perf_counter()
    window.open_meeting("long")
    QApplication.processEvents()
    open_s = time.perf_counter() - started
    assert window.saved_list.count() == count
    assert window.saved_list.item(0).text().startswith("[00:00] Mic")
    assert window.saved_list.item(count - 1).text().startswith("[1:59:58] System")

    window.search_edit.setText(f"item{count - 1}")
    window.run_search()
    window._open_search_hit(window.search_results.item(0))
    assert window.saved_list.currentRow() == count - 1

    started = time.perf_counter()
    assert window.save_as()
    export_s = time.perf_counter() - started
    body = target.read_text(encoding="utf-8").split("TRANSCRIPT\n----------\n\n", 1)[1]
    labels = [line for line in body.splitlines() if line.startswith("[")]
    assert len(labels) == count and labels[0] == "[00:00] Mic" and labels[-1] == "[1:59:58] System"
    assert body.index(texts[0]) < body.index(texts[count // 2]) < body.index(texts[-1])
    print(f"long transcript: {count} segments, {target.stat().st_size} bytes, open {open_s:.2f} s, "
          f"export {export_s:.2f} s")
    assert open_s < 10 and export_s < 10  # generous bounds, not a performance measurement
    window.close()


@pytest.mark.parametrize("status", ["stopped", "interrupted"])
def test_empty_and_interrupted_meetings_export_honestly(app, tmp_path: Path, status: str) -> None:
    seed(tmp_path / "meetings.db", "e", ended=NOW, texts=(), status=status, title="Nothing said")
    target = tmp_path / "empty.md"
    window, store = window_for(tmp_path, chooser=Chooser((str(target), FILE_FILTERS[1])))
    window.open_meeting("e")
    assert window.saved_list.count() == 0
    assert ("Ended incomplete" if status == "interrupted" else "Stopped normally") in window.saved_coverage.text()
    assert window.save_as()
    text = target.read_text(encoding="utf-8")
    assert "INCOMPLETE TRANSCRIPT" in text and "(No saved text.)" in text  # no finalized/complete record
    window.close()
