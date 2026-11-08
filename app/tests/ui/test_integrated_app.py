"""End to end: the main window with the optional OpenAI minutes installed
(as `run()` does), driven end to end offscreen with fake devices, the tone recognizer,
an in-memory key and a fake provider: record -> mute -> Stop -> saved view -> Generate
MoM -> evidence jump -> Redact (minutes invalidated) -> Delete. No network, no real key."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time  # noqa: E402
from pathlib import Path  # noqa: E402

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QMessageBox  # noqa: E402
from test_desktop_app import Chooser, app, lines, say, wait_until, window_for  # noqa: E402,F401

from meeting_minutes.domain.contracts import Source  # noqa: E402
from meeting_minutes.minutes.credentials import MemoryCredentialStore  # noqa: E402
from meeting_minutes.minutes.provider import FakeMinutesGenerator  # noqa: E402
from meeting_minutes.minutes.service import MinutesService  # noqa: E402
from meeting_minutes.ui.desktop import install_minutes  # noqa: E402

KEY = "sk-integration-canary-NOT-REAL"


def test_record_stop_generate_minutes_jump_redact_and_delete(app, tmp_path: Path, monkeypatch) -> None:
    generator = FakeMinutesGenerator()
    window, store = window_for(tmp_path, chooser=Chooser(None))  # Save As offered after Stop: cancelled
    service = MinutesService(store, MemoryCredentialStore(), lambda creds: generator)
    workspace = install_minutes(window, service)
    assert window.workspace_tabs.count() == 3 and "not set up" in window.optional_label.text()
    service.save_key(KEY)
    assert generator.envelopes == []  # saving a key sends nothing

    window.title_edit.setText("Integrated meeting")
    QTest.mouseClick(window.start_button, Qt.MouseButton.LeftButton)
    say(window, Source.SYSTEM, [(0.3, "hello"), (2.0, "deadline")])
    say(window, Source.MIC, [(1.0, "budget")])
    wait_until(lambda: any(line.endswith("\nbudget") for line in lines(window.live_list)))
    window.toggle_mic()  # mute: system continues
    assert not window.session.mic_included
    wait_until(lambda: any(line.endswith("\ndeadline") for line in lines(window.live_list)))
    time.sleep(1.2)
    window.stop_meeting()
    wait_until(lambda: window.pages.currentWidget() is window.saved_page)
    meeting = window._open_meeting
    assert [s.text for s in store.segments(meeting)] == ["hello", "budget", "deadline"]

    workspace.generate()
    wait_until(lambda: workspace.service.versions(meeting))
    envelope = generator.envelopes[-1]
    sent = [s["text"] for s in envelope["segments"]]
    assert sent == ["hello", "budget", "deadline"] and KEY not in str(envelope)
    target = envelope["segments"][2]["id"]
    workspace.evidence_requested.emit(target)  # an evidence chip
    wait_until(lambda: window.saved_list.currentItem() is not None)
    assert window.saved_list.currentItem().text().endswith("\ndeadline")

    asked: list[str] = []

    def yes(parent, title, text, *a):
        asked.append(text)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", yes)
    window.saved_list.setCurrentRow(0)
    window.redact_selected()
    assert "Generated minutes and action items for this meeting are removed" in asked[-1]
    assert all(v.row.invalidated for v in service.versions(meeting))
    assert store.minutes_items(service.versions(meeting)[0].row.id) == []  # content removed
    window.delete_open_meeting()
    assert store.meeting(meeting) is None and store.minutes_versions(meeting) == []
    assert KEY.encode() not in (tmp_path / "meetings.db").read_bytes()
    window._force_close = True
    window.close()
