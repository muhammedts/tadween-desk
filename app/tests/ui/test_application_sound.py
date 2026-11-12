"""Application capture in the desktop window (offscreen): the three computer-sound choices in
New meeting, the separate microphone with its mute/unmute, and a short transcript
saved to History and exported. Fake devices and a fake application deliver tones in
real time; the tone-word recognizer turns them into words."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time  # noqa: E402
from collections.abc import Callable  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from speech_fakes import EnergyVad, ToneRecognizer, scripted  # noqa: E402

from meeting_minutes.capture.app_loopback import UNSUPPORTED  # noqa: E402
from meeting_minutes.capture.fake import DEFAULT_FAKE_DEVICES, FAKE_APP_DEVICES, RealtimeFakeBackend  # noqa: E402
from meeting_minutes.domain.contracts import Source  # noqa: E402
from meeting_minutes.session.meeting import EngineInfo  # noqa: E402
from meeting_minutes.speech import qwen  # noqa: E402,F401 - imported before the network guard patches socket
from meeting_minutes.storage.store import TranscriptStore  # noqa: E402
from meeting_minutes.ui import copy as C  # noqa: E402
from meeting_minutes.ui.desktop import FILE_FILTERS, Engine, MainWindow  # noqa: E402

MIC = "fake:mic-48k-mono"
OUTPUT = "fake:system-44k1-stereo"
APP = FAKE_APP_DEVICES[0].device_id
RATES = {MIC: 48_000, OUTPUT: 44_100, APP: 48_000}


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
    def __init__(self, *answers) -> None:
        self.answers = list(answers)

    def __call__(self, parent, suggested: str):
        return self.answers.pop(0) if self.answers else None


class Backends:
    """Backend factory that remembers every backend it made (the window makes one per
    refresh and one per meeting) and can mark applications unsupported."""

    def __init__(self, *, unsupported: bool = False, fail_open: set[str] | None = None) -> None:
        self.unsupported = unsupported
        self.fail_open = fail_open or set()

    def __call__(self) -> RealtimeFakeBackend:
        backend = RealtimeFakeBackend(devices=DEFAULT_FAKE_DEVICES + (() if self.unsupported else FAKE_APP_DEVICES))
        backend.application_error = UNSUPPORTED if self.unsupported else None
        backend.fail_open |= self.fail_open
        return backend


def window_for(tmp_path: Path, backends: Backends | None = None, chooser: Chooser | None = None):
    store = TranscriptStore(tmp_path / "meetings.db")
    window = MainWindow(store, backends or Backends(), fake_engine, stop_budget_s=5.0,
                        save_dialog=chooser or Chooser())
    window.show()
    wait_until(lambda: "ready" in window.model_label.text())
    window.mic_combo.setCurrentIndex(window.mic_combo.findData(MIC))
    return window, store


def choose(window: MainWindow, mode: str, device: str | None = None) -> None:
    window.sound_mode.setCurrentIndex(window.sound_mode.findData(mode))
    if device is not None:
        combo = window.app_combo if mode == "app" else window.system_combo
        combo.setCurrentIndex(combo.findData(device))


def open_handles(window: MainWindow) -> list:
    return [h for h in window.session_backend.opened if not h.closed]


def say(window: MainWindow, source: Source, words: list[tuple[float, str]]) -> None:
    handle = [h for h in open_handles(window) if h.source is source][-1]
    rate = RATES[handle._spec.device_id]
    elapsed = handle._next_index / rate
    handle.signal = scripted(rate, [(elapsed + t, w) for t, w in words])


def test_new_meeting_offers_three_computer_sound_choices_and_a_separate_microphone(app, tmp_path: Path) -> None:
    window, _ = window_for(tmp_path)
    assert [window.sound_mode.itemData(i) for i in range(window.sound_mode.count())] == ["app", "device", "none"]
    assert [window.sound_mode.itemText(i) for i in range(window.sound_mode.count())] == [t for _, t in C.SOUND_MODES]
    assert window.sound_mode.currentData() == "device"  # today's behaviour is the starting point
    # The mic keeps its own picker and label; applications never appear in it.
    assert window.mic_combo.findData(APP) == -1 and window.system_combo.findData(APP) == -1
    assert window.system_combo.findData(None) == -1  # "None" is the third choice, not a device

    choose(window, "app")
    assert window.app_field.isVisibleTo(window) and not window.system_field.isVisibleTo(window)
    assert window.app_combo.itemText(window.app_combo.findData(APP)) == "Fake player application"
    assert window.app_combo.currentIndex() == -1  # nothing is chosen on the user's behalf
    assert window.start_button.property("blocked") and window.start_hint.text() == C.START_WHY_APP
    window.start_button.click()
    assert window.session is None and window.start_error.text() == C.START_WHY_APP

    window.app_combo.setCurrentIndex(window.app_combo.findData(APP))
    assert not window.start_button.property("blocked")
    assert window.system_item.title.text() == C.READY_SYSTEM["app"]
    assert C.SOUND_MODE_HELP["app"] == window.sound_help.text()

    choose(window, "none")
    assert not window.app_field.isVisibleTo(window) and not window.system_field.isVisibleTo(window)
    assert window.system_item.title.text() == C.READY_SYSTEM["none"] and not window.start_button.property("blocked")
    # Picking an output device is that choice.
    window.system_combo.setCurrentIndex(window.system_combo.findData(OUTPUT))
    assert window.sound_mode.currentData() == "device"
    window.close()


def test_application_sound_through_mute_and_unmute_then_saved_and_exported(app, tmp_path: Path) -> None:
    out = tmp_path / "exports"
    out.mkdir()
    window, store = window_for(tmp_path, chooser=Chooser((str(out / "app.txt"), FILE_FILTERS[0])))
    choose(window, "app", APP)
    window.title_edit.setText("Application sound test")
    window.start_button.click()
    assert window.pages.currentWidget() is window.live_page
    assert sorted((h.source.value, h._spec.device_id) for h in open_handles(window)) == [
        ("mic", MIC), ("system", APP)]
    assert window.source_cards["system"].note.text() == C.APP_NOTE
    assert window._session_devices["system"] == "Fake player application"

    say(window, Source.SYSTEM, [(0.3, "hello"), (2.6, "deadline"), (4.6, "budget")])
    say(window, Source.MIC, [(1.0, "again")])
    wait_until(lambda: any(line.endswith("\nagain") for line in lines(window.live_list)))
    window.toggle_mic()  # mute: the mic closes, the application keeps transcribing
    assert not window.session.mic_included
    assert [h.source for h in open_handles(window)] == [Source.SYSTEM]
    wait_until(lambda: any(line.endswith("\ndeadline") for line in lines(window.live_list)))
    window.toggle_mic()  # unmute: a new mic stream; the application stream is the same one
    assert window.session.mic_included
    say(window, Source.MIC, [(0.3, "مرحبا")])
    wait_until(lambda: any(line.endswith("\nمرحبا") for line in lines(window.live_list)))
    wait_until(lambda: any(line.endswith("\nbudget") for line in lines(window.live_list)))
    assert [h._spec.device_id for h in window.session_backend.opened if h.source is Source.SYSTEM] == [APP]
    time.sleep(0.8)
    window.stop_meeting()
    wait_until(lambda: window.pages.currentWidget() is window.saved_page)
    wait_until(lambda: (out / "app.txt").exists())

    words = [s.split("\n")[1] for s in lines(window.saved_list)]
    assert words == ["hello", "again", "deadline", "مرحبا", "budget"]
    assert store.meeting(window.session.meeting_id).status == "stopped"
    exported = (out / "app.txt").read_text(encoding="utf-8")
    assert exported.startswith("Application sound test\n")
    assert [w for w in exported.splitlines() if w in words] == words
    window.show_history()
    assert [line.split("   ")[1] for line in lines(window.meeting_list)] == ["Application sound test"]
    window.close()


@pytest.mark.parametrize("mode", ["device", "none"])
def test_the_existing_choices_still_work(app, tmp_path: Path, mode: str) -> None:
    window, _ = window_for(tmp_path)
    choose(window, mode, OUTPUT if mode == "device" else None)
    window.start_button.click()
    expected = [("mic", MIC)] + ([("system", OUTPUT)] if mode == "device" else [])
    assert sorted((h.source.value, h._spec.device_id) for h in open_handles(window)) == expected
    assert window.source_cards["system"].note.text() == C.SYSTEM_NOTE
    say(window, Source.MIC, [(0.3, "again")])
    if mode == "device":
        say(window, Source.SYSTEM, [(1.2, "budget")])
    wait_until(lambda: any(line.endswith("\nagain") for line in lines(window.live_list)))
    if mode == "device":
        wait_until(lambda: any(line.endswith("\nbudget") for line in lines(window.live_list)))
    time.sleep(0.6)
    window.stop_meeting()
    wait_until(lambda: window.pages.currentWidget() is window.saved_page)
    assert "again" in [s.split("\n")[1] for s in lines(window.saved_list)]
    window.close()


def test_a_closed_application_shows_the_problem_and_is_never_replaced(app, tmp_path: Path) -> None:
    window, _ = window_for(tmp_path)
    choose(window, "app", APP)
    window.start_button.click()
    window.session_backend.fail_open.add(APP)  # the program has exited: it cannot be found again
    [h for h in open_handles(window) if h.source is Source.SYSTEM][0].lose()
    wait_until(lambda: "m:lost:system" in window.alerts.keys())
    banner = window.alerts.banners["m:lost:system"]
    assert banner.title.text() == C.APP_LOST_TITLE
    assert "Fake player application" in banner.body.text() and "never switches" in banner.body.text()
    window._reconnect("system")
    assert "could not be reopened" in window.live_health.text()
    assert [h._spec.device_id for h in window.session_backend.opened if h.source is Source.SYSTEM] == [APP]
    assert window.session.mic_included and [h.source for h in open_handles(window)] == [Source.MIC]
    window.stop_meeting()
    wait_until(lambda: window.pages.currentWidget() is window.saved_page)
    # The missing application time is an explicit gap in the saved transcript.
    assert "00:00–00:00 System: device lost" in lines(window.events_list)
    assert any("capture losses=1" in line for line in lines(window.events_list))
    window.close()


def test_without_process_loopback_the_other_two_choices_remain(app, tmp_path: Path) -> None:
    window, _ = window_for(tmp_path, Backends(unsupported=True))
    app_item = window.sound_mode.model().item(window.sound_mode.findData("app"))
    assert not app_item.isEnabled() and window.app_problem.text() == C.APP_UNSUPPORTED
    for mode in ("device", "none"):
        assert window.sound_mode.model().item(window.sound_mode.findData(mode)).isEnabled()
    choose(window, "device", OUTPUT)
    assert not window.start_button.property("blocked")
    window.close()
