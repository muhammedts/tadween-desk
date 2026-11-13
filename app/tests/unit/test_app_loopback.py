"""Application audio capture: program listing, identity, routing, and the
controller with an application as the computer-sound source.

The native process-loopback path is exercised by `test_native_*` below against a
silent child process (nothing is played), and audibly by scripts/app_loopback_probe.py
with two programs playing different test tones (see the handoff)."""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from meeting_minutes.capture.app_loopback import (
    APPLICATION_CLOSED,
    BACKEND,
    MIN_WINDOWS_BUILD,
    UNSUPPORTED,
    AppLoopbackBackend,
    AppLoopbackHandle,
    ProcessInfo,
    RoutedBackend,
    clean_name,
    device_id_for,
    group_applications,
    is_application_id,
    parse_device_id,
    process_created,
    top_process,
    windows_build,
)
from meeting_minutes.capture.base import DeviceUnavailable
from meeting_minutes.capture.controller import CaptureController
from meeting_minutes.capture.fake import FAKE_APP_DEVICES, FakeAudioBackend, pump_until
from meeting_minutes.domain.contracts import DeviceSelection, Direction, Source
from meeting_minutes.domain.states import MeetingState

from app_loopback_fakes import SlowWindows, run_shutdown  # noqa: I001
from capture_rig import DEVICES, MEETING, MIC, SECOND, SYSTEM, SpySink  # noqa: I001

APP = FAKE_APP_DEVICES[0].device_id

# A browser: main process 100 (window), its audio service 101 and a renderer 102 are
# children; 200 is an unrelated program; 300 is a child whose parent id was reused by
# a newer process (created after the child), so the walk must stop there.
PROCS = {
    100: ProcessInfo(100, 4, "chrome.exe", 1_000),
    101: ProcessInfo(101, 100, "chrome.exe", 1_100),
    102: ProcessInfo(102, 100, "chrome.exe", 1_200),
    200: ProcessInfo(200, 4, "Teams.exe", 2_000),
    250: ProcessInfo(250, 4, "notepad.exe", 9_000),  # reused id: younger than its "child"
    300: ProcessInfo(300, 250, "notepad.exe", 3_000),
    400: ProcessInfo(400, 4, "python.exe", 4_000),  # Tadween itself
    4: ProcessInfo(4, 0, "System", None),
}


def test_windows_of_a_program_group_to_its_top_process() -> None:
    windows = [(102, "Tab A - Chrome"), (100, "Tab B - Chrome"), (200, "Teams"), (102, "Tab A - Chrome")]
    apps = group_applications(PROCS, windows)
    assert [(a.pid, a.exe) for a in apps] == [(100, "chrome.exe"), (200, "Teams.exe")]
    assert apps[0].titles == ("Tab A - Chrome", "Tab B - Chrome")  # deduplicated, in order
    assert apps[0].device_id == device_id_for(100, 1_000)


def test_the_walk_stops_at_another_program_or_a_reused_parent_id() -> None:
    assert top_process(101, PROCS).pid == 100
    assert top_process(200, PROCS).pid == 200  # parent is "System": another program
    assert top_process(300, PROCS).pid == 300  # parent 250 is younger: not its parent
    assert top_process(999, PROCS) is None


def test_tadween_never_lists_itself() -> None:
    apps = group_applications(PROCS, [(400, "Tadween Desk"), (200, "Teams")], exclude_pids={400})
    assert [a.pid for a in apps] == [200]


def test_identity_includes_the_creation_time() -> None:
    device_id = device_id_for(1234, 5678)
    assert is_application_id(device_id) and parse_device_id(device_id) == (1234, 5678)
    assert not is_application_id("wasapi:render:abc") and not is_application_id(None)
    for bad in ("app:1234", "app:x:y", "wasapi:render:abc"):
        with pytest.raises(DeviceUnavailable):
            parse_device_id(bad)


def test_display_names_fall_back_to_the_file_name() -> None:
    assert clean_name("  Microsoft   Teams ", "ms-teams.exe") == "Microsoft Teams"
    assert clean_name(None, "CiscoCollabHost.exe") == "CiscoCollabHost"
    assert clean_name("", "") == "(unnamed program)"
    assert len(clean_name("x" * 500, "a.exe")) == 80


def backend(build: int = MIN_WINDOWS_BUILD, windows=None) -> AppLoopbackBackend:
    return AppLoopbackBackend(
        build=lambda: build, processes=lambda: PROCS,
        windows=lambda: windows if windows is not None else [(101, "Private meeting title - Chrome"), (200, "Teams")],
        describe={100: "Google Chrome", 200: None}.get, own_pid=400)


def test_listing_names_programs_and_keeps_window_titles_out_of_the_descriptor() -> None:
    b = backend()
    listed = b.enumerate()
    assert [(d.display_name, d.backend, d.direction) for d in listed] == [
        ("Google Chrome", BACKEND, Direction.RENDER), ("Teams", BACKEND, Direction.RENDER)]
    assert all("Private" not in d.model_dump_json() for d in listed)  # titles are never stored
    assert b.window_titles(listed[0].device_id) == ("Private meeting title - Chrome",)
    assert b.unsupported_reason is None


def test_an_older_windows_lists_no_applications_and_refuses_to_open() -> None:
    b = backend(build=MIN_WINDOWS_BUILD - 1)
    assert b.enumerate() == [] and b.unsupported_reason == UNSUPPORTED
    with pytest.raises(DeviceUnavailable, match=UNSUPPORTED):
        b.open(DeviceSelection(source=Source.SYSTEM, device_id=device_id_for(200, 2_000)), lambda f: None,
               meeting_id=uuid4(), epoch=1, privacy_generation=lambda: 0, clock=time.monotonic_ns)


def test_an_application_is_never_a_microphone() -> None:
    with pytest.raises(ValueError):
        backend().open(DeviceSelection(source=Source.MIC, device_id=device_id_for(200, 2_000)), lambda f: None,
                       meeting_id=uuid4(), epoch=1, privacy_generation=lambda: 0, clock=time.monotonic_ns)


class Failing:
    def enumerate(self):
        raise OSError("listing failed")


def test_the_router_keeps_devices_and_applications_apart() -> None:
    devices = FakeAudioBackend(meeting_id=MEETING, devices=DEVICES)
    apps = FakeAudioBackend(meeting_id=MEETING, devices=FAKE_APP_DEVICES)
    router = RoutedBackend(devices, apps)
    assert {d.device_id for d in router.enumerate()} == {MIC, SYSTEM, "fake:mic2-16k-mono", APP}
    router.open(DeviceSelection(source=Source.SYSTEM, device_id=APP), lambda f: None, epoch=1)
    router.open(DeviceSelection(source=Source.SYSTEM, device_id=SYSTEM), lambda f: None, epoch=1)
    assert [h._spec.device_id for h in apps.opened] == [APP]
    assert [h._spec.device_id for h in devices.opened] == [SYSTEM]
    # A device id never reaches the application backend: the fake app backend refuses it.
    with pytest.raises(DeviceUnavailable):
        RoutedBackend(devices, apps).applications.open(
            DeviceSelection(source=Source.SYSTEM, device_id=SYSTEM), lambda f: None, epoch=1)


def test_a_failed_application_listing_leaves_the_devices_usable() -> None:
    router = RoutedBackend(FakeAudioBackend(meeting_id=MEETING, devices=DEVICES), Failing())
    assert {d.device_id for d in router.enumerate()} == {MIC, SYSTEM, "fake:mic2-16k-mono"}
    assert router.application_error == "application_list_failed"


def run_controller():
    devices = FakeAudioBackend(meeting_id=MEETING, devices=DEVICES)
    apps = FakeAudioBackend(meeting_id=MEETING, devices=FAKE_APP_DEVICES)
    sink = SpySink()
    controller = CaptureController(RoutedBackend(devices, apps), mic_included=True, sink=sink, clock=lambda: 0,
                                   meeting_id=MEETING)
    sink.controller = controller
    return controller, devices, apps, sink


def test_application_sound_continues_through_mic_mute_and_unmute() -> None:
    controller, devices, apps, sink = run_controller()
    result = controller.start(mic=MIC, system=APP)
    assert result.state is MeetingState.CAPTURING
    handles = lambda: [h for h in devices.opened + apps.opened if not h.closed]  # noqa: E731
    pump_until(handles(), 1 * SECOND)
    controller.process_pending()
    app_before = len(sink.of(Source.SYSTEM))
    assert app_before > 0 and sink.of(Source.MIC)

    controller.set_mic_included(False, expected_generation=controller.privacy_generation)
    assert [h.source for h in handles()] == [Source.SYSTEM]  # the mic stream is closed; the app runs
    pump_until(handles(), 2 * SECOND)
    controller.process_pending()
    mic_while_muted = len(sink.of(Source.MIC))
    assert len(sink.of(Source.SYSTEM)) > app_before

    controller.set_mic_included(True, expected_generation=controller.privacy_generation)
    pump_until(handles(), 3 * SECOND)
    controller.process_pending()
    assert len(sink.of(Source.MIC)) > mic_while_muted
    assert all(s.admitted_at_accept for s in sink.seen)
    assert [h._spec.device_id for h in apps.opened] == [APP]  # one application stream, never reopened
    controller.stop()


def test_a_closed_application_is_reported_and_never_replaced() -> None:
    controller, devices, apps, sink = run_controller()
    controller.start(mic=MIC, system=APP)
    apps.opened[0].lose()  # the application exits
    controller.check_health()
    assert controller.source_state(Source.SYSTEM).value == "lost"
    assert controller.state is MeetingState.DEGRADED
    apps.fail_open.add(APP)  # it is gone: reconnecting cannot find it
    result = controller.reconnect(Source.SYSTEM)
    assert result.outcome.value == "failed" and result.effective == APP
    assert [h._spec.device_id for h in devices.opened] == [MIC]  # no output device was opened instead
    controller.stop()


# ----------------------------------------------------------- native (silent)

native = pytest.mark.skipif(sys.platform != "win32" or windows_build() < MIN_WINDOWS_BUILD,
                            reason="process loopback needs Windows 10 build 20348+")


@native
def test_native_capture_of_a_silent_program_delivers_silence_then_detects_its_exit() -> None:
    """Opens process loopback on a child that renders nothing (nothing is played)."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        time.sleep(0.3)
        created = process_created(child.pid)
        assert created is not None
        device_id = device_id_for(child.pid, created)
        frames = []
        handle = AppLoopbackBackend().open(DeviceSelection(source=Source.SYSTEM, device_id=device_id),
                                           frames.append, meeting_id=uuid4(), epoch=1, privacy_generation=lambda: 0,
                                           clock=time.monotonic_ns)
        time.sleep(1.0)
        assert handle.is_active() and frames
        assert all(f.sample_rate == 48_000 and f.channels == 2 and f.source is Source.SYSTEM for f in frames)
        assert max(float(abs(f.data).max()) for f in frames) == 0.0
        child.kill()
        child.wait()
        time.sleep(0.2)
        assert not handle.is_active()  # the controller turns this into a "lost" source
        handle.close()
        with pytest.raises(DeviceUnavailable, match=APPLICATION_CLOSED):
            AppLoopbackBackend().open(DeviceSelection(source=Source.SYSTEM, device_id=device_id), lambda f: None,
                                      meeting_id=uuid4(), epoch=1, privacy_generation=lambda: 0,
                                      clock=time.monotonic_ns)
    finally:
        child.kill()
        child.wait()


@native
def test_native_open_refuses_a_reused_process_id() -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        time.sleep(0.3)
        created = process_created(child.pid)
        wrong = device_id_for(child.pid, (created or 0) + 1)  # same id, different process
        with pytest.raises(DeviceUnavailable, match=APPLICATION_CLOSED):
            AppLoopbackBackend().open(DeviceSelection(source=Source.SYSTEM, device_id=wrong), lambda f: None,
                                      meeting_id=uuid4(), epoch=1, privacy_generation=lambda: 0,
                                      clock=time.monotonic_ns)
    finally:
        child.kill()
        child.wait()


# ------------------- Activation ownership, late completion, shutdown, reclaim

win32 = pytest.mark.skipif(sys.platform != "win32", reason="ctypes COM thunks are Windows-only")


@pytest.fixture
def al():
    from meeting_minutes.capture import app_loopback

    assert app_loopback.pending_activations() == 0  # every test starts with all slots free
    yield app_loopback


def track(monkeypatch, al) -> list:
    made: list = []
    real = al._allocate

    def allocate():
        made.append(real())
        return made[-1]

    monkeypatch.setattr(al, "_allocate", allocate)
    return made


@win32
def test_a_completion_after_the_timeout_is_cleaned_up_after_the_capture_handle_is_gone(monkeypatch, al) -> None:
    import functools
    import gc
    import os

    windows = SlowWindows(delay=0.4)
    made = track(monkeypatch, al)
    monkeypatch.setattr(al, "_activate", functools.partial(al._activate, timeout=0.05, activate=windows))
    pid = os.getpid()
    handle = AppLoopbackHandle(pid=pid, created=process_created(pid), source=Source.SYSTEM, meeting_id=uuid4(),
                               epoch=1, callback=lambda f: None, privacy_generation=lambda: 0,
                               clock=time.monotonic_ns)
    with pytest.raises(DeviceUnavailable, match="timed out"):
        handle.start()  # bounded: Start gives up without waiting for Windows
    activation = made[0]
    assert not activation.done.is_set() and activation.abandoned and al.pending_activations() == 1
    del handle
    gc.collect()  # the failed capture handle is gone; the handler slot and thunks are not
    assert windows.completed.wait(5)  # Windows calls back late, through the native pointer
    assert windows.client.released == 1 and activation.late_clients_released == 1  # discarded, never captured
    assert windows.operation.released == 1  # the operation is released exactly once
    assert activation.refs == 0 and activation.reclaimed and al.pending_activations() == 0


@win32
@pytest.mark.parametrize("activation_hr, call_hr", [(0, 0), (-2147467259, 0), (0, -2147467259)])
def test_every_result_path_releases_the_operation_once(monkeypatch, al, activation_hr: int, call_hr: int) -> None:
    windows = SlowWindows(delay=0.0, activation_hr=activation_hr, call_hr=call_hr)
    made = track(monkeypatch, al)
    if activation_hr == 0 and call_hr == 0:
        client = al._activate(1234, timeout=5, activate=windows)
        assert client == windows.client.pointer and windows.client.released == 0  # the caller owns it
        al._release(client)
    else:
        with pytest.raises(DeviceUnavailable, match="activation failed"):
            al._activate(1234, timeout=5, activate=windows)
    assert windows.completed.wait(5)
    assert windows.operation.released == 1
    # A client handed out with a failure is released, never used; a failed activation hands out none.
    assert windows.client.released == (0 if activation_hr < 0 else 1)
    assert made[0].refs == 0 and made[0].reclaimed and al.pending_activations() == 0


@win32
def test_a_refused_activation_holds_nothing(monkeypatch, al) -> None:
    made = track(monkeypatch, al)
    with pytest.raises(DeviceUnavailable, match="refused"):
        al._activate(1234, timeout=5, activate=lambda *a: -2147024891)  # E_ACCESSDENIED
    assert made[0].refs == 0 and made[0].finished and made[0].reclaimed and al.pending_activations() == 0


@win32
def test_repeated_attempts_reclaim_every_finished_request(al) -> None:
    """250 refusals, 50 completions and 20 late completions leave nothing behind."""
    import gc
    import tracemalloc

    def attempts() -> None:
        for _ in range(250):
            with pytest.raises(DeviceUnavailable):
                al._activate(1234, timeout=5, activate=lambda *a: -2147024891)
        for _ in range(50):
            windows = SlowWindows(delay=0.0)
            al._release(al._activate(1234, timeout=5, activate=windows))
            assert windows.completed.wait(5)
        for _ in range(20):
            windows = SlowWindows(delay=0.05)
            with pytest.raises(DeviceUnavailable, match="timed out"):
                al._activate(1234, timeout=0.0, activate=windows)
            assert windows.completed.wait(5)
            assert windows.client.released == 1 and windows.operation.released == 1

    attempts()  # warm-up: first-use allocations (prototypes, caches) are not growth
    gc.collect()
    tracemalloc.start()
    before = tracemalloc.take_snapshot()
    attempts()
    gc.collect()
    after = tracemalloc.take_snapshot()
    tracemalloc.stop()
    growth = sum(stat.size_diff for stat in after.compare_to(before, "filename"))
    assert al.pending_activations() == 0 and len(al._STATE) == 0
    assert growth < 64 * 1024, growth  # retaining every request cost about 1.24 MB


@win32
def test_pending_requests_are_bounded_and_late_completions_still_discard_their_stream(al) -> None:
    """Requests Windows has not finished hold a slot; the bound refuses more
    instead of growing, and each slot comes back when its late completion arrives."""
    stuck = []
    for _ in range(al.MAX_PENDING):
        windows = SlowWindows(delay=None)  # Windows keeps it for now
        with pytest.raises(DeviceUnavailable, match="timed out"):
            al._activate(1234, timeout=0.0, activate=windows)
        stuck.append(windows)
    assert al.pending_activations() == al.MAX_PENDING
    extra = SlowWindows(delay=0.0)
    with pytest.raises(DeviceUnavailable, match=al.BUSY):
        al._activate(1234, timeout=5, activate=extra)
    assert extra.calls == 0  # refused before asking Windows; nothing new is held
    for slot, windows in zip(al._SLOT_ADDRESSES, stuck, strict=True):  # Windows finishes them late
        al._method(slot, 3, ctypes.c_long, ctypes.c_void_p)(windows.operation.pointer)
        al._method(slot, 2, ctypes.c_ulong)()
        assert windows.client.released == 1 and windows.operation.released == 1
    assert al.pending_activations() == 0
    again = SlowWindows(delay=0.0)
    al._release(al._activate(1234, timeout=5, activate=again))  # a slot is free again
    assert again.completed.wait(5) and al.pending_activations() == 0


def _tests_dir() -> str:
    from pathlib import Path

    return str(Path(__file__).resolve().parents[1])


@win32
@pytest.mark.parametrize("delay", ["never", "1.5"])
def test_shutdown_never_lets_a_late_callback_into_python_teardown(delay: str) -> None:
    """Windows still holds the handler when the grace period ends (it never
    completes, or completes 1.5 s later). The exit guard ends the process before
    teardown: the intended exit code, no crash, and the callback never runs."""
    proc = run_shutdown(delay, 0.2, _tests_dir())
    assert proc.returncode == 7, (proc.returncode, proc.stdout, proc.stderr)
    assert "start gave up: process loopback activation timed out" in proc.stdout
    assert "callback entering handler" not in proc.stdout
    assert "normal teardown continued" not in proc.stdout  # teardown never began
    assert "ending without interpreter teardown" in proc.stderr


@win32
@pytest.mark.parametrize("streams", ["none", "closed", "failing"])
def test_shutdown_ends_the_process_without_usable_console_streams(streams: str, tmp_path: Path) -> None:
    """The CMD launcher starts pythonw, which has no stdout or stderr. A missing, closed or
    failing stream must not keep the guard from ending the process: with Windows still
    holding a handler, normal teardown (the marker) never begins."""
    marker = tmp_path / "teardown.txt"
    proc = run_shutdown("never", 0.2, _tests_dir(), streams=streams, marker=marker)
    assert proc.returncode == 7
    assert not marker.exists()


def test_the_marker_shows_teardown_when_nothing_is_pending(tmp_path: Path) -> None:
    """The marker itself works: once Windows has let go, exit continues and writes it."""
    marker = tmp_path / "teardown.txt"
    proc = run_shutdown("0.3", 3.0, _tests_dir(), streams="none", marker=marker)
    assert proc.returncode == 7
    assert marker.read_text() == "normal teardown continued"


def test_shutdown_continues_normally_when_windows_lets_go_within_the_grace_period() -> None:
    proc = run_shutdown("0.3", 3.0, _tests_dir())
    assert proc.returncode == 7, (proc.returncode, proc.stdout, proc.stderr)
    out = proc.stdout
    assert out.index("start gave up") < out.index("callback entering handler") < out.index("normal teardown continued")
    assert "ending without interpreter teardown" not in proc.stderr
