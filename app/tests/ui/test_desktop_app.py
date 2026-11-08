"""Desktop window driven end to end, offscreen: fake devices delivering in real time,
tone-word recognizer, energy VAD, real session, store and PySide6 widgets.

Not covered here: real screen rendering, screen readers, DPI scaling and real
devices/models; those need a hands-on test on a Windows PC."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time  # noqa: E402
from collections.abc import Callable  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox  # noqa: E402
from speech_fakes import EnergyVad, ToneRecognizer, scripted  # noqa: E402

from meeting_minutes.capture.fake import RealtimeFakeBackend  # noqa: E402
from meeting_minutes.domain.contracts import Source  # noqa: E402
from meeting_minutes.session.meeting import EngineInfo  # noqa: E402
from meeting_minutes.speech import qwen  # noqa: E402 - imported before the network guard patches socket
from meeting_minutes.storage.store import TranscriptStore  # noqa: E402
from meeting_minutes.ui.desktop import FILE_FILTERS, Engine, MainWindow  # noqa: E402

MIC = "fake:mic-48k-mono"
SYSTEM = "fake:system-44k1-stereo"
RATES = {Source.MIC: 48_000, Source.SYSTEM: 44_100}


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def fake_engine(mode: str) -> Engine:
    return Engine(ToneRecognizer(), EnergyVad, EngineInfo("fake-tone-engine", "0" * 64), mode)


def wait_until(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached in time")


def lines(widget) -> list[str]:
    return [widget.item(i).text() for i in range(widget.count())]


class Chooser:
    """Stands in for Windows' Save As dialog: returns queued answers, (path, filter) or
    None for Cancel, and records each suggested path."""

    def __init__(self, *answers: tuple[str, str] | None) -> None:
        self.answers = list(answers)
        self.suggested: list[str] = []

    def __call__(self, parent, suggested: str) -> tuple[str, str] | None:
        self.suggested.append(suggested)
        return self.answers.pop(0) if self.answers else None


def window_for(tmp_path: Path, loader=fake_engine, *, chooser: Chooser | None = None, stop_budget_s: float = 5.0,
               **kwargs) -> tuple[MainWindow, TranscriptStore]:
    store = TranscriptStore(tmp_path / "meetings.db")
    window = MainWindow(store, RealtimeFakeBackend, loader, stop_budget_s=stop_budget_s,
                        save_dialog=chooser or Chooser(), **kwargs)
    window.show()
    wait_until(lambda: "ready" in window.model_label.text())
    window.mic_combo.setCurrentIndex(window.mic_combo.findData(MIC))
    window.system_combo.setCurrentIndex(window.system_combo.findData(SYSTEM))
    return window, store


def say(window: MainWindow, source: Source, words: list[tuple[float, str]]) -> None:
    handle = [h for h in window.session_backend.opened if h.source is source and not h.closed][-1]
    elapsed = handle._next_index / RATES[source]  # schedule relative to now
    handle.signal = scripted(RATES[source], [(elapsed + t, w) for t, w in words])


def test_start_mute_stop_then_history_search_correct_and_delete(app, tmp_path: Path, monkeypatch) -> None:
    window, store = window_for(tmp_path)
    window.title_edit.setText("UI test meeting")
    QTest.mouseClick(window.start_button, Qt.MouseButton.LeftButton)
    assert window.pages.currentWidget() is window.live_page
    say(window, Source.SYSTEM, [(0.3, "hello"), (2.4, "deadline")])
    say(window, Source.MIC, [(1.0, "budget")])
    wait_until(lambda: any(line.endswith("\nhello") for line in lines(window.live_list)))
    wait_until(lambda: any(line.endswith("\nbudget") for line in lines(window.live_list)))

    QTest.keyClick(window, Qt.Key.Key_M, Qt.KeyboardModifier.ControlModifier)  # keyboard mute
    assert not window.session.mic_included
    assert window.mic_button.text().startswith("Unmute mic") and "excluded" in window.mic_banner.text()
    # Exclusion closes the mic stream: there is nothing left to transcribe from.
    assert not [h for h in window.session_backend.opened if h.source is Source.MIC and not h.closed]
    wait_until(lambda: any(line.endswith("\ndeadline") for line in lines(window.live_list)))
    time.sleep(1.2)
    QTest.mouseClick(window.stop_button, Qt.MouseButton.LeftButton)
    wait_until(lambda: window.pages.currentWidget() is window.saved_page)

    saved = lines(window.saved_list)
    assert [s.split("\n")[1] for s in saved] == ["hello", "budget", "deadline"]
    assert "Complete" in window.saved_coverage.text() and "Mic excluded 1 time" in window.saved_coverage.text()
    assert any("mic excluded" in line for line in lines(window.events_list))
    meeting_id = window.session.meeting_id
    assert store.meeting(meeting_id).status == "stopped"

    window.show_history()
    assert [line.split("   ")[1] for line in lines(window.meeting_list)] == ["UI test meeting"]
    window.search_edit.setText("deadline")
    QTest.keyClick(window.search_edit, Qt.Key.Key_Return)
    assert len(lines(window.search_results)) == 1
    window._open_search_hit(window.search_results.item(0))
    assert window.saved_list.currentItem().text().endswith("\ndeadline")

    monkeypatch.setattr(QInputDialog, "getMultiLineText", lambda *a, **k: ("the deadline moved", True))
    window.correct_selected()
    assert window.saved_list.currentItem().text().endswith("\nthe deadline moved")
    assert "corrected" in window.saved_list.currentItem().text()
    assert [r.origin for r in store.revisions(window._selected_segment())] == ["asr", "user"]

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    window.saved_list.setCurrentRow(0)
    window.redact_selected()
    assert [s.split("\n")[1] for s in lines(window.saved_list)] == ["budget", "the deadline moved"]
    assert any("text redacted" in line for line in lines(window.events_list))
    window.delete_open_meeting()
    assert store.list_meetings() == [] and window.pages.currentWidget() is window.history_page
    window.close()


def test_close_and_save_finalizes_then_offers_save_as_and_closes(app, tmp_path: Path, monkeypatch) -> None:
    """The window stays open through finalization and the file choice, then closes."""
    out = tmp_path / "exports"
    out.mkdir()
    chooser = Chooser((str(out / "closing"), FILE_FILTERS[0]))  # no extension typed: .txt is added
    window, store = window_for(tmp_path, chooser=chooser)
    QTest.mouseClick(window.start_button, Qt.MouseButton.LeftButton)
    say(window, Source.SYSTEM, [(0.3, "again")])
    wait_until(lambda: any(line.endswith("\nagain") for line in lines(window.live_list)))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    window.close()
    assert window.isVisible() and window.session.state.value == "finalizing"
    wait_until(lambda: not window.isVisible())
    assert window.session.finished.is_set() and len(chooser.suggested) == 1
    assert Path(chooser.suggested[0]).suffix == ".txt"
    [meeting] = store.list_meetings()
    assert meeting.status == "stopped" and [s.text for s in store.segments(meeting.id)] == ["again"]
    text = (out / "closing.txt").read_text(encoding="utf-8")
    assert "System\nagain\n" in text and "INCOMPLETE" not in text


def test_cancelling_save_as_on_close_keeps_the_window_open_with_history_and_capture_stopped(
        app, tmp_path: Path, monkeypatch) -> None:
    chooser = Chooser(None)  # Cancel
    window, store = window_for(tmp_path, chooser=chooser)
    QTest.mouseClick(window.start_button, Qt.MouseButton.LeftButton)
    say(window, Source.SYSTEM, [(0.3, "again")])
    wait_until(lambda: any(line.endswith("\nagain") for line in lines(window.live_list)))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    window.close()
    wait_until(lambda: "not closed" in window.saved_notice.text())
    assert window.isVisible() and window.pages.currentWidget() is window.saved_page
    assert len(chooser.suggested) == 1 and [p.name for p in (tmp_path).iterdir() if p.suffix in (".txt", ".md")] == []
    assert window.session.state.value == "stopped" and all(h.closed for h in window.session_backend.opened)
    assert [s.text for s in store.segments(window.session.meeting_id)] == ["again"]  # History kept it
    assert lines(window.saved_list)[0].endswith("\nagain")  # ready for Save As again
    window.close()  # a second close just closes: the meeting is already saved
    assert not window.isVisible() and len(chooser.suggested) == 1


def test_missing_speech_files_block_start_with_a_setup_message(app, tmp_path: Path) -> None:
    from meeting_minutes.speech.sherpa import SpeechAssetsMissing

    def missing(mode: str) -> Engine:
        raise SpeechAssetsMissing({"ready": False, "roles": [{"role": "stt", "problems": ["encoder:missing"]}]})

    store = TranscriptStore(tmp_path / "meetings.db")
    window = MainWindow(store, RealtimeFakeBackend, missing)
    wait_until(lambda: "SETUP REQUIRED" in window.model_label.text())
    window.system_combo.setCurrentIndex(window.system_combo.findData(SYSTEM))
    QTest.mouseClick(window.start_button, Qt.MouseButton.LeftButton)
    assert window.session is None and window.pages.currentWidget() is window.preflight_page
    assert store.list_meetings() == []
    window.close()


def test_recovered_meetings_are_announced_and_capture_is_not_reopened(app, tmp_path: Path) -> None:
    store = TranscriptStore(tmp_path / "meetings.db")
    store.create_meeting("m1", title="crashed", timezone_name="UTC")
    store.set_status("m1", "capturing")
    window = MainWindow(store, RealtimeFakeBackend, fake_engine, recovered=store.recover_unfinished())
    assert "1 meeting(s) were still running" in window.recovered_label.text()
    assert window.session is None and window.pages.currentWidget() is window.preflight_page
    window.show_history()
    assert "Ended incomplete" in lines(window.meeting_list)[0]
    window.close()


# ------------------------------------- History, deletion, a full disk, abandoned decodes


def finished_meeting(window: MainWindow, words: list[tuple[float, str]]) -> str:
    QTest.mouseClick(window.start_button, Qt.MouseButton.LeftButton)
    say(window, Source.SYSTEM, words)
    last = words[-1][1]
    wait_until(lambda: any(line.endswith("\n" + last) for line in lines(window.live_list)))
    window.stop_meeting()
    wait_until(lambda: window.pages.currentWidget() is window.saved_page)
    return window.session.meeting_id


def test_search_results_follow_corrections_and_late_events_of_a_deleted_meeting_are_ignored(
        app, tmp_path: Path, monkeypatch) -> None:
    window, store = window_for(tmp_path)
    meeting_id = finished_meeting(window, [(0.3, "deadline")])
    assert window.live_list.count() == 0  # the saved view is the one copy shown
    window.show_history()
    window.search_edit.setText("deadline")
    window.run_search()
    window._open_search_hit(window.search_results.item(0))
    monkeypatch.setattr(QInputDialog, "getMultiLineText", lambda *a, **k: ("moved to friday", True))
    window.correct_selected()
    window.show_history()
    assert lines(window.search_results) == ["No matches in current transcript text or titles."]

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    window.open_meeting(meeting_id)
    window.delete_open_meeting()
    window._on_session_event("final", {"meeting": meeting_id, "id": "x", "source": "system", "start_ms": 0,
                                       "end_ms": 1, "language": "en", "text": "late text", "hypothesis_id": "s:0"})
    assert window.live_list.count() == 0 and window.session is None
    window.close()


def test_a_failed_deletion_is_reported_and_the_meeting_stays(app, tmp_path: Path, monkeypatch) -> None:
    from meeting_minutes.storage.store import StorageError

    window, store = window_for(tmp_path)
    meeting_id = finished_meeting(window, [(0.3, "hello")])
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    warnings: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _p, _t, text, *a, **k: warnings.append(text))

    def refuse(_meeting_id):
        raise StorageError("disk_full")

    monkeypatch.setattr(store, "delete_meeting", refuse)
    assert not window.delete_meeting(meeting_id)
    assert "could not be saved (disk_full)" in warnings[0]
    assert [m.id for m in store.list_meetings()] == [meeting_id]
    window.close()


def test_a_full_disk_shows_paused_and_resume_stays_refused_until_saving_works(app, tmp_path: Path,
                                                                             monkeypatch) -> None:
    from meeting_minutes.storage.store import StorageError

    window, store = window_for(tmp_path)
    QTest.mouseClick(window.start_button, Qt.MouseButton.LeftButton)
    full = {"on": True}
    add_final = store.add_final

    def maybe_full(*args, **kwargs):
        if full["on"]:
            raise StorageError("disk_full")
        return add_final(*args, **kwargs)

    monkeypatch.setattr(store, "add_final", maybe_full)
    say(window, Source.SYSTEM, [(0.3, "budget")])
    wait_until(lambda: window.session.save_blocked == "disk_full")
    wait_until(lambda: "saving failed" in window.live_state.text())
    assert any("NOT SAVED" in line for line in lines(window.live_list))
    window.toggle_pause()  # Resume while still full
    assert window.session.state.value == "paused" and "Still cannot save" in window.live_health.text()
    full["on"] = False
    window.toggle_pause()
    assert window.session.state.value == "capturing"
    wait_until(lambda: [s.text for s in store.segments(window.session.meeting_id)] == ["budget"])
    window.stop_meeting()
    wait_until(lambda: window.pages.currentWidget() is window.saved_page)
    assert store.meeting(window.session.meeting_id).status == "stopped"
    window.close()


def test_an_engine_left_decoding_is_never_reused_and_blocks_start_while_busy(app, tmp_path: Path) -> None:
    window, store = window_for(tmp_path)
    finished_meeting(window, [(0.3, "hello")])
    mode = window.language_combo.currentData()
    engine = window._engines[mode]

    class Busy:
        in_flight: object = {"source": "system"}

        def status(self):
            return {"in_flight": self.in_flight}

    busy = Busy()
    window._quarantine.append((engine, busy))  # as after a stop that abandoned a decode
    window._engines.pop(mode)
    window._on_engine_loaded(mode, fake_engine(mode), "")  # a fresh engine is ready
    window.show_preflight()
    QTest.mouseClick(window.start_button, Qt.MouseButton.LeftButton)
    assert "still finishing" in window.start_error.text() and window.pages.currentWidget() is window.preflight_page
    busy.in_flight = None  # the abandoned decode returned
    QTest.mouseClick(window.start_button, Qt.MouseButton.LeftButton)
    assert window.pages.currentWidget() is window.live_page
    assert window.session.pipeline._recognizer is not engine.recognizer  # the old one is not reused
    window.stop_meeting()
    wait_until(lambda: window.session.finished.is_set())
    window.close()


# ------------------------------------------------------- Save As and stalled saves


def test_stop_offers_save_as_and_the_saved_view_exports_the_corrected_revision_in_utf8(
        app, tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "exports"
    out.mkdir()
    chooser = Chooser((str(out / "first.txt"), FILE_FILTERS[0]), (str(out / "second"), FILE_FILTERS[1]))
    window, store = window_for(tmp_path, chooser=chooser)
    window.title_edit.setText("اجتماع الميزانية / budget: Q4?")
    meeting_id = finished_meeting(window, [(0.3, "مرحبا"), (1.6, "budget"), (2.9, "الميزانية")])
    wait_until(lambda: (out / "first.txt").exists())
    suggested = Path(chooser.suggested[0]).name
    assert suggested.startswith("اجتماع الميزانية - budget- Q4") and suggested.endswith(".txt")
    first = (out / "first.txt").read_bytes()
    text = first.decode("utf-8")
    assert text.startswith("اجتماع الميزانية / budget: Q4?\n")
    assert [line for line in text.splitlines() if line in ("مرحبا", "budget", "الميزانية")] == [
        "مرحبا", "budget", "الميزانية"]
    assert "Saved a copy as “first.txt”" in window.saved_notice.text()

    window.saved_list.setCurrentRow(1)
    monkeypatch.setattr(QInputDialog, "getMultiLineText", lambda *a, **k: ("الميزانية budget approved", True))
    window.correct_selected()
    QTest.mouseClick(window.save_as_button, Qt.MouseButton.LeftButton)  # Save As from the saved view
    md_lines = (out / "second.md").read_text(encoding="utf-8").splitlines()  # Markdown chosen, extension added
    revision = store.meeting(meeting_id).transcript_revision
    assert any(line.startswith(f"- **Saved revision:** {revision} (exported") for line in md_lines)
    assert any(line.endswith("System · corrected**  ") for line in md_lines)
    assert "الميزانية budget approved" in md_lines and "budget" not in md_lines  # only the current revision
    assert (out / "first.txt").read_bytes() == first  # the earlier file is untouched
    assert len(chooser.suggested) == 2 and Path(chooser.suggested[1]).parent == out  # remembers the folder
    window.close()


def test_overwrite_is_confirmed_and_a_failed_write_keeps_the_old_file_and_history(
        app, tmp_path: Path, monkeypatch) -> None:
    import os
    import stat

    out = tmp_path / "exports"
    out.mkdir()
    existing = out / "keep.txt"
    existing.write_text("old content", encoding="utf-8")
    locked = out / "locked.txt"
    locked.write_text("locked content", encoding="utf-8")
    os.chmod(locked, stat.S_IREAD)
    chooser = Chooser((str(existing), FILE_FILTERS[0]))  # then Cancel
    answers = [QMessageBox.StandardButton.No]
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: answers.pop(0))
    warnings: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _p, _t, text, *a, **k: warnings.append(text))
    window, store = window_for(tmp_path, chooser=chooser)
    try:
        meeting_id = finished_meeting(window, [(0.3, "hello")])
        wait_until(lambda: len(chooser.suggested) == 2)  # "No" to replacing asks again; then Cancel
        assert existing.read_text(encoding="utf-8") == "old content" and answers == []

        chooser.answers = [(str(locked), FILE_FILTERS[0]), (str(existing), FILE_FILTERS[0])]
        answers[:] = [QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.Yes]
        assert window.save_as()
        assert len(warnings) == 1 and "could not be saved (access_denied)" in warnings[0]
        assert str(out) not in warnings[0]  # fixed code, no path
        assert locked.read_text(encoding="utf-8") == "locked content"
        assert "System\nhello\n" in existing.read_text(encoding="utf-8")
        assert sorted(p.name for p in out.iterdir()) == ["keep.txt", "locked.txt"]  # no temporary files left
        assert [s.text for s in store.segments(meeting_id)] == ["hello"]
    finally:
        os.chmod(locked, stat.S_IREAD | stat.S_IWRITE)
        window.close()


def test_save_as_marks_an_incomplete_meeting_and_lists_unsaved_text_apart(app, tmp_path: Path, monkeypatch) -> None:
    from meeting_minutes.storage.store import StorageError

    out = tmp_path / "exports"
    out.mkdir()
    chooser = Chooser((str(out / "incomplete.txt"), FILE_FILTERS[0]))
    window, store = window_for(tmp_path, chooser=chooser)
    QTest.mouseClick(window.start_button, Qt.MouseButton.LeftButton)

    def full(*args, **kwargs):
        raise StorageError("disk_full")

    monkeypatch.setattr(store, "add_final", full)
    say(window, Source.SYSTEM, [(0.3, "budget")])
    wait_until(lambda: window.session.save_blocked == "disk_full")
    window.stop_meeting()  # still full at Stop
    wait_until(lambda: (out / "incomplete.txt").exists())
    text = (out / "incomplete.txt").read_text(encoding="utf-8")
    saved_part, _, unsaved_part = text.partition("NOT SAVED IN HISTORY")
    assert "INCOMPLETE TRANSCRIPT" in saved_part and "(No saved text.)" in saved_part
    assert "budget" not in saved_part and "NOT SAVED (disk_full)\nbudget" in unsaved_part
    assert store.meeting(window.session.meeting_id).status == "interrupted"
    window.close()


@pytest.mark.parametrize("ending", ["stop", "close"])
def test_a_stalled_save_leaves_the_window_responsive_and_save_as_waits_for_a_safe_read(
        app, tmp_path: Path, monkeypatch, ending: str) -> None:
    """A save stuck inside a real store operation (holding the store) must
    not freeze the window; the file is offered once the saved transcript can be read."""
    import threading

    out = tmp_path / "exports"
    out.mkdir()
    chooser = Chooser((str(out / "late.txt"), FILE_FILTERS[0]))
    window, store = window_for(tmp_path, chooser=chooser, stop_budget_s=1.0)
    QTest.mouseClick(window.start_button, Qt.MouseButton.LeftButton)
    meeting_id = window.session.meeting_id
    say(window, Source.SYSTEM, [(0.3, "hello")])
    wait_until(lambda: [s.text for s in store.segments(meeting_id)] == ["hello"])
    entered, release = threading.Event(), threading.Event()
    add_final = store.add_final

    def stalled(*args, **kwargs):
        with store._lock:  # stuck on the disk while holding the store, as SQLite would be
            entered.set()
            release.wait(30)
        return add_final(*args, **kwargs)

    monkeypatch.setattr(store, "add_final", stalled)
    questions = [QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No]  # close: stop and save; then wait
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: questions.pop(0))
    infos: list[str] = []
    monkeypatch.setattr(QMessageBox, "information", lambda _p, _t, text, *a, **k: infos.append(text))
    say(window, Source.SYSTEM, [(0.3, "budget")])
    try:
        assert entered.wait(10)
        if ending == "stop":
            window.stop_meeting()
        else:
            window.close()
        wait_until(lambda: "NOT fully saved" in window.saved_coverage.text())
        assert not release.is_set() and window.session.state.value == "interrupted" and window.isVisible()
        if ending == "close":
            wait_until(lambda: not questions)  # asked whether to close anyway; answered "wait"
            assert window.isVisible()
        started = time.monotonic()
        window.show_history()  # the store is still held by the stalled save
        assert lines(window.search_results) == ["History could not be read (busy)."]
        window.open_meeting(meeting_id)
        assert not window.save_as() and "still being saved" in infos[-1]
        window.show_preflight()
        QTest.mouseClick(window.start_button, Qt.MouseButton.LeftButton)
        assert "still being saved" in window.start_error.text()
        assert time.monotonic() - started < 2.0 and chooser.suggested == []
    finally:
        release.set()
    if ending == "close":
        wait_until(lambda: not window.isVisible())  # file chosen, then the window closed
    else:
        wait_until(lambda: (out / "late.txt").exists())
        assert "Ended incomplete" in window.saved_title.text()
        assert [line.split("\n")[1] for line in lines(window.saved_list)] == ["hello", "budget"]
    text = (out / "late.txt").read_text(encoding="utf-8")
    assert "INCOMPLETE TRANSCRIPT" in text and "System\nhello\n" in text and "System\nbudget\n" in text
    assert store.meeting(meeting_id).status == "interrupted"  # a late save never claims a normal stop
    window._force_close = True
    window.close()


def test_the_language_choices_say_what_each_mode_does(app, tmp_path: Path) -> None:
    """Automatic picks one language per chunk of speech, not per sentence, and the
    forced modes rewrite the other language; the selector says so."""
    window, _ = window_for(tmp_path)
    labels = {window.language_combo.itemData(i): window.language_combo.itemText(i)
              for i in range(window.language_combo.count())}
    # Automatic (policy A) stays the default; R is a separate, explicitly experimental choice.
    assert set(labels) == {"mixed", "mixed_r", "en", "ar"} and window.language_combo.currentData() == "mixed"
    assert window.language_combo.itemData(0) == "mixed" and "experimental" in labels["mixed_r"]
    assert "per sentence" not in labels["mixed"]
    note = window.language_combo.toolTip()
    assert note == window.language_combo.accessibleDescription()
    assert "language of each stretch of speech" in note and "Arabic letters" in note and "marked" in note
    window.close()


class MarkingRecognizer(ToneRecognizer):
    """The tone recognizer, with R's "language uncertain" mark on every line."""

    def transcribe(self, chunk, guard=None):
        from meeting_minutes.speech.pipeline import RecognizedText

        result = super().transcribe(chunk)
        return RecognizedText(result.text, "portuguese", ("lang_uncertain",))

    accepts_guard = True


def test_quality_marks_are_shown_live_and_in_the_saved_transcript(app, tmp_path: Path) -> None:
    """A recognizer's quality mark reaches the live line and the saved line, and does
    not make the meeting incomplete."""
    loader = lambda mode: Engine(MarkingRecognizer(), EnergyVad,  # noqa: E731
                                 EngineInfo("llama.cpp-b11960/qwen3-asr-0.6b-q8_0-gguf;language=auto;policy=R",
                                            "0" * 64), mode)
    window, _ = window_for(tmp_path, loader)
    QTest.mouseClick(window.start_button, Qt.MouseButton.LeftButton)
    say(window, Source.SYSTEM, [(0.3, "hello")])
    wait_until(lambda: any(line.endswith("\nhello") for line in lines(window.live_list)))
    assert any("System · language uncertain\nhello" in line for line in lines(window.live_list))
    time.sleep(1.2)
    QTest.mouseClick(window.stop_button, Qt.MouseButton.LeftButton)
    wait_until(lambda: window.pages.currentWidget() is window.saved_page)
    assert [line.split("\n")[0].split("] ")[1] for line in lines(window.saved_list)] == ["System · language uncertain"]
    assert "Complete" in window.saved_coverage.text()
    window.close()


# ------------------------------------------------ a dead speech server is not "ready"


class FakeChild:
    def __init__(self) -> None:
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -1

    def wait(self, timeout=None):
        return self.returncode


def qwen_window(tmp_path: Path, monkeypatch, failures: list[str]):
    """The window with the real QwenService and its cache, a fake owned child per start
    (`failures` makes the next starts fail with those codes) and no model or socket."""
    from meeting_minutes.models.manifest import default_manifest_path, load_manifest
    from meeting_minutes.ui.desktop import QWEN_MODES

    children: list[FakeChild] = []

    def start(self, *args, cancel=None, **kwargs):
        if failures:
            raise RuntimeError(failures.pop(0))
        self.process = FakeChild()
        self.api_key, self.tied_to_app = "k", True
        children.append(self.process)
        return self

    monkeypatch.setattr(qwen.QwenServer, "start", start)
    path = default_manifest_path()
    service = qwen.QwenService(load_manifest(path), path.parent, port=12345)
    service._check = lambda *a, **k: {"ready": True}

    def loader(mode: str) -> Engine:
        language, policy = QWEN_MODES[mode]
        info = EngineInfo(qwen.engine_id(service.base_id, language, policy), service.model_hash)
        return Engine(service.recognizer(language, policy), EnergyVad, info, mode)

    window, store = window_for(tmp_path, loader)
    return window, store, service, children


def test_a_dead_speech_server_reloads_every_cached_choice_through_one_restart(app, tmp_path, monkeypatch) -> None:
    window, _, service, children = qwen_window(tmp_path, monkeypatch, [])
    window.language_combo.setCurrentIndex(window.language_combo.findData("mixed_r"))
    wait_until(lambda: "mixed_r" in window._engines)
    first = window._engines["mixed"].recognizer.server
    assert window._engines["mixed_r"].recognizer.server is first and len(children) == 1
    children[0].returncode = 1  # the server exits between meetings
    window.show_preflight()  # R is selected: it reloads, and the restart is shared
    wait_until(lambda: "mixed_r" in window._engines)
    window.language_combo.setCurrentIndex(window.language_combo.findData("mixed"))  # switch back
    wait_until(lambda: "mixed" in window._engines)
    engines = window._engines
    assert len(children) == 2 and engines["mixed"].recognizer.server is engines["mixed_r"].recognizer.server
    assert engines["mixed"].recognizer.server is not first and engines["mixed"].recognizer.server.alive()
    assert engines["mixed_r"].info.engine_id.endswith("policy=R") and engines["mixed"].recognizer.policy == "A"
    window.close()
    service.close(timeout_s=0.1)


def test_start_never_uses_a_dead_engine_and_a_failed_restart_can_be_retried(app, tmp_path, monkeypatch) -> None:
    failures: list[str] = []
    window, _, service, children = qwen_window(tmp_path, monkeypatch, failures)
    children[0].returncode = 1
    failures.append("qwen_server_exited_3")
    QTest.mouseClick(window.start_button, Qt.MouseButton.LeftButton)  # refused: no stale "ready" engine
    assert window.session is None and "not ready" in window.start_error.text()
    wait_until(lambda: "could not be started (qwen_server_exited_3)" in window.model_label.text())
    assert "mixed" not in window._engines
    QTest.mouseClick(window.start_button, Qt.MouseButton.LeftButton)  # Start tries again
    wait_until(lambda: "ready" in window.model_label.text())
    assert window._engines["mixed"].recognizer.server.alive() and len(children) == 2
    window.close()
    service.close(timeout_s=0.1)
