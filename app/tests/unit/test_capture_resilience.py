"""WASAPI capture robustness with synthetic devices: backend initialisation, ambiguous
loopback endpoints, downstream sink failures and input overflow gaps."""

from __future__ import annotations

import threading
from uuid import UUID

import numpy as np
import pytest
from capture_rig import SECOND, Rig
from fake_pyaudio import device, make_module

from meeting_minutes.capture.base import AmbiguousDevice, DeviceUnavailable
from meeting_minutes.capture.controller import CaptureController
from meeting_minutes.capture.wasapi import AMBIGUOUS_LOOPBACK, WasapiBackend, fingerprint
from meeting_minutes.domain.contracts import DeviceSelection, Direction, Source
from meeting_minutes.domain.events import CaptureGapPayload, GapReason

MEETING = UUID(int=2002101)


def open_on(backend, source, device_id):
    return backend.open(
        DeviceSelection(source=source, device_id=device_id), lambda f: None,
        meeting_id=MEETING, epoch=1, privacy_generation=lambda: 0, clock=lambda: 0,
    )


def run_with_timeout(fn, seconds=5.0):
    result, error = [], []

    def target():
        try:
            result.append(fn())
        except BaseException as exc:
            error.append(exc)

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), "call did not return (deadlock)"
    if error:
        raise error[0]
    return result[0]


# Backend initialisation -----------------------------------------------------------


def test_fresh_backend_opens_a_saved_fingerprint_without_prior_enumerate():
    module = make_module()
    backend = WasapiBackend(pyaudio_module=module)
    mic = fingerprint(Direction.CAPTURE, "Headset Microphone (Poly BT700)")
    handle = run_with_timeout(lambda: open_on(backend, Source.MIC, mic))
    assert module.streams[-1].kwargs["input_device_index"] == 16
    handle.close()
    backend.close()


def test_resolve_on_a_fresh_backend_and_reopen_after_close_do_not_hang():
    module = make_module()
    backend = WasapiBackend(pyaudio_module=module)
    speakers = fingerprint(Direction.RENDER, "Speakers (Realtek(R) Audio)")
    assert run_with_timeout(lambda: backend.resolve(speakers)).display_name == "Speakers (Realtek(R) Audio)"
    backend.close()
    handle = run_with_timeout(lambda: open_on(backend, Source.SYSTEM, speakers))  # re-initialises
    assert len(module.instances) == 2 and module.instances[0].terminated
    handle.close()
    backend.close()


def test_audio_system_initialisation_failure_is_reported_not_raised_or_hung():
    module = make_module()

    def broken():
        raise OSError("PortAudio initialisation failed")

    module.PyAudio = broken
    backend = WasapiBackend(pyaudio_module=module)
    assert run_with_timeout(backend.enumerate) == []
    assert backend.init_error == "audio_system_unavailable"
    with pytest.raises(DeviceUnavailable):
        run_with_timeout(lambda: open_on(backend, Source.MIC, fingerprint(Direction.CAPTURE, "x")))
    assert module.streams == []


# Ambiguous loopback endpoints -----------------------------------------------------


def renders(backend):
    return [d for d in backend.enumerate() if d.direction is Direction.RENDER]


@pytest.mark.parametrize("second_rate", [48_000.0, 44_100.0], ids=["same-format", "different-format"])
def test_duplicate_playback_names_are_listed_unavailable_and_refuse_to_open(second_rate):
    module = make_module([
        device(1, "USB speakers", outputs=2),
        device(2, "USB speakers", outputs=2),
        device(3, "USB speakers [Loopback]", inputs=2, loopback=True),
        device(4, "USB speakers [Loopback]", inputs=2, rate=second_rate, loopback=True),
        device(5, "Speakers (Realtek(R) Audio)", outputs=2),
        device(6, "Speakers (Realtek(R) Audio) [Loopback]", inputs=2, loopback=True),
    ], default_input=-1, default_output=1)
    backend = WasapiBackend(pyaudio_module=module)
    found = renders(backend)
    usb = [d for d in found if d.display_name == "USB speakers"]
    assert len(usb) == 2 and len({d.device_id for d in usb}) == 2
    for d in usb:
        assert not d.available and not d.supports_loopback and d.probed_formats == ()
        assert d.unavailable_reason == AMBIGUOUS_LOOPBACK
        with pytest.raises(AmbiguousDevice):
            open_on(backend, Source.SYSTEM, d.device_id)
    assert module.streams == []
    unique = next(d for d in found if d.display_name.startswith("Speakers"))
    assert unique.available and unique.unavailable_reason is None
    open_on(backend, Source.SYSTEM, unique.device_id).close()
    assert module.streams[-1].kwargs["input_device_index"] == 6


def test_one_endpoint_with_two_same_named_loopbacks_is_also_refused():
    module = make_module([
        device(1, "Speakers", outputs=2),
        device(2, "Speakers [Loopback]", inputs=2, loopback=True),
        device(3, "Speakers [Loopback]", inputs=2, loopback=True),
    ], default_input=-1, default_output=1)
    [speakers] = renders(WasapiBackend(pyaudio_module=module))
    assert not speakers.available and speakers.unavailable_reason == AMBIGUOUS_LOOPBACK


def test_refresh_after_a_duplicate_disappears_makes_the_remaining_endpoint_usable():
    module = make_module([
        device(1, "USB speakers", outputs=2),
        device(2, "USB speakers", outputs=2),
        device(3, "USB speakers [Loopback]", inputs=2, loopback=True),
        device(4, "USB speakers [Loopback]", inputs=2, loopback=True),
    ], default_input=-1, default_output=1)
    backend = WasapiBackend(pyaudio_module=module)
    old_ids = [d.device_id for d in renders(backend)]
    module.devices = [module.devices[0], module.devices[2]]  # one pair unplugged
    [remaining] = renders(backend)
    assert remaining.available and remaining.device_id == fingerprint(Direction.RENDER, "USB speakers")
    with pytest.raises(AmbiguousDevice):  # ordinal ids from the old snapshot are not reused
        open_on(backend, Source.SYSTEM, old_ids[0])


# Downstream sink failures ---------------------------------------------------------


class FlakySink:
    controller = None

    def __init__(self, fail_from: int, fail_until: int | None = None, source: Source | None = None):
        self.calls = 0
        self.fail_from, self.fail_until, self.source = fail_from, fail_until, source

    def accept(self, frame):
        if self.source is not None and frame.source is not self.source:
            return
        self.calls += 1
        if self.calls > self.fail_from and (self.fail_until is None or self.calls <= self.fail_until):
            raise RuntimeError("synthetic downstream failure")

    def reset(self, source):
        return 0


def test_failing_sink_records_each_consumed_frame_as_an_exact_gap():
    rig = Rig(sink=FlakySink(fail_from=50, fail_until=60, source=Source.MIC))
    rig.start()
    rig.run(SECOND)
    failed = [g for g in rig.controller.gaps if g.reason is GapReason.PROCESSING_FAILED]
    assert [(g.source, g.start_ms, g.end_ms) for g in failed] == [
        (Source.MIC, ms, ms + 10) for ms in range(500, 600, 10)
    ]
    status = rig.controller.status()
    assert status["sources"]["mic"]["frames_failed_downstream"] == 10
    assert status["sources"]["mic"]["frames_forwarded"] == 90  # failures are not forwarded
    assert status["sources"]["system"]["frames_forwarded"] == 100  # the other source continued
    assert status["sink_errors"] == 10


def test_failing_sink_during_stop_makes_the_drain_incomplete():
    rig = Rig(sink=FlakySink(fail_from=0))
    rig.start()
    rig.run(SECOND, process=False)
    result = rig.controller.stop()
    assert result.failed_frames == 200 and result.drained_frames == 0
    assert not result.drain_completed and result.undrained_frames == 0
    lost = {s: sum(g.end_ms - g.start_ms for g in rig.controller.gaps if g.source is s) for s in Source}
    assert lost == {Source.MIC: 1000, Source.SYSTEM: 1000}


# Input overflow and discontinuities -----------------------------------------------


def wasapi_controller():
    module = make_module()
    backend = WasapiBackend(pyaudio_module=module)
    mic = next(d for d in backend.enumerate() if d.display_name.startswith("Microphone Array"))
    now = [0]
    controller = CaptureController(backend, mic_included=True, clock=lambda: now[0], meeting_id=MEETING)
    controller.start(mic=mic.device_id, system=None)
    callback = module.streams[-1].callback
    buffer = np.zeros(960, dtype=np.float32).tobytes()

    def deliver(at_ns, status=0):
        now[0] = at_ns
        callback(buffer, 480, {}, status)
        controller.process_pending()

    return controller, deliver, module.paInputOverflow


def gap_list(controller):
    return [(g.reason, g.start_ms, g.end_ms, g.duration_known) for g in controller.gaps]


def test_ordinary_jitter_reports_no_overflow_or_gap():
    controller, deliver, _ = wasapi_controller()
    for i, jitter in enumerate([0, 3, -2, 7, 0]):
        deliver(10_000_000 + i * 10_000_000 + jitter * 1_000_000)
    assert gap_list(controller) == []
    assert controller.status()["sources"]["mic"]["input_overflows"] == 0
    controller.close()


def test_short_overflow_is_a_point_marker_of_unknown_length():
    controller, deliver, overflow = wasapi_controller()
    deliver(10_000_000)  # first buffer right after Start
    deliver(40_000_000, overflow)  # within jitter: the amount discarded is unknown
    [(reason, start, end, known)] = gap_list(controller)
    assert reason is GapReason.INPUT_OVERFLOW and start == end and known is False
    assert controller.status()["sources"]["mic"]["input_overflows"] == 1
    assert controller.status()["sources"]["mic"]["lost_ms"] == {"input_overflow": 0}
    controller.close()


def test_long_discontinuity_is_overflow_with_driver_report_and_no_data_without():
    controller, deliver, overflow = wasapi_controller()
    deliver(10_000_000)
    deliver(520_000_000, overflow)  # 0.5 s missing, driver reported overflow
    deliver(1_030_000_000)          # another 0.5 s missing, no report
    reasons = [(r, end - start, known) for r, start, end, known in gap_list(controller)]
    assert reasons == [(GapReason.INPUT_OVERFLOW, 500, True), (GapReason.NO_DATA_DELIVERED, 500, True)]
    controller.close()


def test_unknown_length_gap_must_be_a_point():
    with pytest.raises(ValueError):
        CaptureGapPayload(source=Source.MIC, epoch=1, start_ms=0, end_ms=5,
                          reason=GapReason.INPUT_OVERFLOW, duration_known=False)
