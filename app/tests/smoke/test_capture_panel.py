"""The tkinter capture panel driven through its real widgets with the fake backend."""

from __future__ import annotations

import tkinter as tk

import pytest
from capture_rig import DEVICES, MEETING, MIC2

from meeting_minutes.capture.fake import FakeAudioBackend, FakeDeviceSpec
from meeting_minutes.domain.contracts import AudioFormat, Direction, Source
from meeting_minutes.domain.states import SourceState
from meeting_minutes.ui.capture_panel import CapturePanel


@pytest.fixture(scope="module")
def tk_root():
    # One Tk interpreter per process: re-creating Tk() after destroying it fails on
    # this CPython/Tk build, so each test gets its own Toplevel window instead.
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # reported as a skip with its reason, never silent
        pytest.skip(f"no Tk display available: {exc}")
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def panel(tk_root):
    root = tk.Toplevel(tk_root)
    root.withdraw()
    backend = FakeAudioBackend(meeting_id=MEETING, devices=DEVICES)
    asked = []
    p = CapturePanel(root, backend, poll_ms=10_000, ask_degraded=lambda text: asked.append(text) or True)
    p.asked = asked
    yield p
    if not p.closed:
        p.on_close()


def test_panel_starts_nothing_until_start_is_pressed(panel):
    assert panel.backend.opened == []
    assert panel.start_button.instate(["!disabled"])
    assert panel.stop_button.instate(["disabled"])
    assert panel.mic_banner.cget("text") == "Mic included in transcription"


def test_panel_mute_pause_stop_through_widgets(panel):
    panel.start_button.invoke()
    assert panel.controller.state.value == "capturing"
    assert {h.source for h in panel.backend.opened} == {Source.MIC, Source.SYSTEM}
    panel.mic_button.invoke()
    assert not panel.controller.mic_included
    assert panel.mic_banner.cget("text").startswith("Mic excluded from transcription")
    assert panel.mic_button.cget("text") == "Unmute mic"
    # Ctrl+M is bound for every widget; a withdrawn test window cannot take keyboard
    # focus, so check the binding targets the handler and call it as Tk would.
    assert "_on_shortcut" in panel.root.bind_all("<Control-m>")
    panel._on_shortcut(None)
    assert panel.controller.mic_included
    panel.pause_button.invoke()
    assert panel.controller.state.value == "paused" and panel.pause_button.cget("text") == "Resume"
    panel.pause_button.invoke()
    assert panel.controller.state.value == "capturing"
    panel.stop_button.invoke()
    assert panel.controller.state.value == "stopped"
    assert all(h.closed for h in panel.backend.opened)


def test_panel_mute_before_start_is_honoured(panel):
    panel.mic_button.invoke()
    assert panel.mic_banner.cget("text").startswith("Mic excluded")
    panel.start_button.invoke()
    assert [h.source for h in panel.backend.opened] == [Source.SYSTEM]


def test_failed_source_asks_once_for_degraded_start(panel):
    panel.backend.fail_open.add(panel.selected(Source.SYSTEM))
    panel.start_button.invoke()
    assert len(panel.asked) == 1 and "System audio" in panel.asked[0]
    assert panel.controller.state.value == "degraded"


def test_closing_the_window_stops_capture_and_releases_devices(panel):
    panel.start_button.invoke()
    handles = list(panel.backend.opened)
    panel.on_close()
    assert all(h.closed for h in handles)
    assert panel.controller.state.value == "stopped"


# "(none)" cannot be applied silently during capture.


@pytest.mark.parametrize("source", [Source.MIC, Source.SYSTEM])
def test_applying_none_during_capture_is_refused_and_the_picker_restored(panel, source):
    panel.start_button.invoke()
    before = {s: panel.controller.selected_device(s) for s in Source}
    combo = panel.mic_choice if source is Source.MIC else panel.system_choice
    combo.current(0)  # "(none)"
    panel.apply_button.invoke()
    assert panel.selected(source) == before[source]  # the picker shows the device in use
    assert panel.controller.source_state(source).value == "running"
    assert "cannot be removed during capture" in panel.notice.get()
    other = Source.SYSTEM if source is Source.MIC else Source.MIC
    assert panel.controller.selected_device(other) == before[other]
    assert panel.controller.source_state(other).value == "running"
    assert panel.controller.mic_included


def test_applying_none_while_paused_is_refused_and_none_is_allowed_for_the_next_start(panel):
    panel.start_button.invoke()
    panel.pause_button.invoke()
    panel.system_choice.current(0)
    panel.apply_button.invoke()
    assert panel.selected(Source.SYSTEM) == panel.controller.selected_device(Source.SYSTEM)
    assert "cannot be removed" in panel.notice.get()
    panel.stop_button.invoke()
    assert panel.apply_button.instate(["disabled"])
    panel.system_choice.current(0)  # before a new start, "(none)" is an ordinary choice
    panel.start_button.invoke()
    assert panel.controller.selected_device(Source.SYSTEM) is None
    assert panel.controller.source_state(Source.MIC).value == "running"
    assert panel.notice.get() == ""


# After a switch the picker shows the effective device.

SYSTEM2 = "fake:system2-48k-stereo"
SECOND_DEVICE = {Source.MIC: MIC2, Source.SYSTEM: SYSTEM2}


@pytest.fixture
def panel2(tk_root):
    root = tk.Toplevel(tk_root)
    root.withdraw()
    devices = (*DEVICES, FakeDeviceSpec(SYSTEM2, "Second fake playback (48 kHz stereo)", Direction.RENDER,
                                        AudioFormat(sample_rate=48_000, channels=2), 550.0, 480))
    p = CapturePanel(root, FakeAudioBackend(meeting_id=MEETING, devices=devices), poll_ms=10_000)
    yield p
    if not p.closed:
        p.on_close()


def pick(panel, source, device_id):
    combo, ids = panel._combo(source)
    combo.current(ids.index(device_id))
    panel.apply_button.invoke()


def other(source):
    return Source.SYSTEM if source is Source.MIC else Source.MIC


@pytest.mark.parametrize("source", [Source.MIC, Source.SYSTEM])
def test_successful_switch_shows_the_new_device_without_a_notice(panel2, source):
    panel2.start_button.invoke()
    pick(panel2, source, SECOND_DEVICE[source])
    assert panel2.selected(source) == panel2.controller.selected_device(source) == SECOND_DEVICE[source]
    assert panel2.notice.get() == ""


@pytest.mark.parametrize("source", [Source.MIC, Source.SYSTEM])
def test_failed_switch_restores_the_picker_and_explains_the_rollback(panel2, source):
    panel2.start_button.invoke()
    previous = panel2.controller.selected_device(source)
    panel2.backend.fail_open.add(SECOND_DEVICE[source])
    pick(panel2, source, SECOND_DEVICE[source])
    assert panel2.selected(source) == panel2.controller.selected_device(source) == previous
    assert panel2.controller.source_state(source) is SourceState.RUNNING
    assert "could not be opened (device_unavailable); still capturing from the previous device" in panel2.notice.get()
    assert panel2.controller.source_state(other(source)) is SourceState.RUNNING
    assert panel2.controller.mic_included


@pytest.mark.parametrize("source", [Source.MIC, Source.SYSTEM])
def test_both_devices_failing_is_shown_and_applying_again_retries(panel2, source):
    panel2.start_button.invoke()
    previous = panel2.controller.selected_device(source)
    panel2.backend.fail_open.update({previous, SECOND_DEVICE[source]})
    pick(panel2, source, SECOND_DEVICE[source])
    assert panel2.selected(source) == previous
    assert panel2.controller.source_state(source) is SourceState.FAILED
    assert "this source is not capturing" in panel2.notice.get()
    assert panel2.controller.source_state(other(source)) is SourceState.RUNNING
    panel2.backend.fail_open.clear()
    panel2.apply_button.invoke()  # same device, failed source: retried
    assert panel2.controller.source_state(source) is SourceState.RUNNING
    assert panel2.selected(source) == previous and panel2.notice.get() == ""


def test_switch_while_paused_is_not_reported_as_tested(panel2):
    panel2.start_button.invoke()
    panel2.pause_button.invoke()
    opened = len(panel2.backend.opened)
    pick(panel2, Source.SYSTEM, SYSTEM2)
    assert panel2.selected(Source.SYSTEM) == panel2.controller.selected_device(Source.SYSTEM) == SYSTEM2
    assert "has not been opened yet and will be tried on Resume" in panel2.notice.get()
    assert len(panel2.backend.opened) == opened
    panel2.pause_button.invoke()
    assert panel2.controller.source_state(Source.SYSTEM) is SourceState.RUNNING


def test_switch_while_mic_excluded_keeps_it_excluded_and_says_when_it_opens(panel2):
    panel2.start_button.invoke()
    panel2.mic_button.invoke()
    pick(panel2, Source.MIC, MIC2)
    assert panel2.selected(Source.MIC) == panel2.controller.selected_device(Source.MIC) == MIC2
    assert "will be tried when the mic is unmuted" in panel2.notice.get()
    assert not panel2.controller.mic_included
    assert panel2.controller.source_state(Source.SYSTEM) is SourceState.RUNNING


def test_device_picked_while_paused_that_fails_on_resume_is_reported(panel2):
    panel2.start_button.invoke()
    panel2.pause_button.invoke()
    pick(panel2, Source.SYSTEM, SYSTEM2)
    panel2.backend.fail_open.add(SYSTEM2)
    panel2.pause_button.invoke()  # Resume
    assert panel2.controller.source_state(Source.SYSTEM) is SourceState.FAILED
    assert panel2.selected(Source.SYSTEM) == panel2.controller.selected_device(Source.SYSTEM) == SYSTEM2
    assert "System audio could not be opened on Resume (device_unavailable)" in panel2.notice.get()
    assert panel2.controller.source_state(Source.MIC) is SourceState.RUNNING


def test_mic_picked_while_excluded_that_fails_on_unmute_is_reported(panel2):
    panel2.start_button.invoke()
    panel2.mic_button.invoke()
    pick(panel2, Source.MIC, MIC2)
    panel2.backend.fail_open.add(MIC2)
    panel2.mic_button.invoke()  # Unmute
    assert panel2.controller.mic_included
    assert panel2.controller.source_state(Source.MIC) is SourceState.FAILED
    assert "Microphone could not be opened on unmute (device_unavailable)" in panel2.notice.get()
    panel2.backend.fail_open.clear()
    panel2.mic_button.invoke()
    panel2.mic_button.invoke()  # exclude and include again: the notice clears once it opens
    assert panel2.controller.source_state(Source.MIC) is SourceState.RUNNING and panel2.notice.get() == ""
