"""Mic mute/unmute privacy boundary, on synthetic capture.

A canary sample value marks microphone audio captured after the exclusion was
acknowledged (or still pending when it happened). It must never reach downstream
input (the spy sink / fake decoder), and no result stamped with an old privacy
generation may commit, while system audio continues throughout.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest
from capture_rig import CANARY, MEETING, MIC, MIC2, SECOND, FakeDecoder, Rig, canary_signal

from meeting_minutes.capture.controller import CaptureController
from meeting_minutes.domain.contracts import AudioFrame, Source, TimingQuality
from meeting_minutes.domain.events import MicPrivacyStateEvent
from meeting_minutes.domain.privacy import CommandOrigin, LocalConfirmationRequired, StaleGenerationError
from meeting_minutes.domain.states import MeetingState, SourceState


def mic_frame(generation: int, *, index: int = 0, value=CANARY, epoch: int = 1) -> AudioFrame:
    return AudioFrame(
        meeting_id=MEETING, source=Source.MIC, epoch=epoch, privacy_generation=generation,
        sequence=index, first_sample_index=index * 480, sample_rate=48_000, channels=1,
        monotonic_ns=index * 10_000_000, timing_quality=TimingQuality.SYNTHETIC,
        data=np.full((480, 1), value, dtype=np.float32),
    )


def assert_no_canary_downstream(rig):
    assert not any(s.canary for s in rig.sink.seen)
    assert all(s.admitted_at_accept for s in rig.sink.seen)


def test_mute_while_system_speaks_system_continues_mic_closed():
    """Muting closes this app's mic stream while system audio keeps flowing, with no gap."""
    rig = Rig()
    rig.start()
    rig.run(SECOND)
    rig.exclude()
    assert rig.controller.source_state(Source.MIC) is SourceState.CLOSED
    assert rig.handle(Source.MIC) is None  # this app's mic stream is closed, not just ignored
    assert rig.controller.state is MeetingState.CAPTURING  # intentional, not degraded
    rig.run(3 * SECOND)
    system = rig.sink.of(Source.SYSTEM)
    assert len(system) == 300 and {s.epoch for s in system} == {1}
    assert not [s for s in rig.sink.of(Source.MIC) if s.monotonic_ns >= SECOND]
    assert rig.controller.gaps == []


def test_canary_in_flight_at_the_callback_is_rejected():
    """A buffer stamped before the mute but delivered after it is rejected at the gate."""
    rig = Rig()
    rig.start()
    rig.run(SECOND)
    stamped_before = rig.controller.privacy_generation
    rig.exclude()
    rig.controller._on_frame(mic_frame(stamped_before, index=101))
    rig.controller.process_pending()
    assert_no_canary_downstream(rig)
    assert rig.controller.boundary.rejected_frames[Source.MIC] == 1


def test_canary_pending_in_the_queue_is_discarded_and_reported():
    """Pending, unforwarded mic audio is cleared on mute and the discarded time reported."""
    rig = Rig()
    rig.start()
    rig.run(SECOND)
    rig.handle(Source.MIC).signal = canary_signal
    rig.run(2 * SECOND, process=False)  # 1 s of canary queued, not yet processed
    rig.exclude()
    rig.run(3 * SECOND)
    assert_no_canary_downstream(rig)
    [event] = [e for e in rig.controller.events() if isinstance(e, MicPrivacyStateEvent)]
    assert event.payload.discarded_pending_ms == 1000
    assert rig.controller.status()["sources"]["mic"]["queued_ms"] == 0


def test_canary_already_downstream_is_reset_and_stale_decode_cannot_commit():
    """A decode already in flight when the mute lands is rejected at commit."""
    decoder = FakeDecoder()
    rig = Rig(sink=decoder)
    decoder.controller = rig.controller
    rig.start()
    rig.handle(Source.MIC).signal = canary_signal
    rig.run(SECOND)
    in_flight = decoder.take(Source.MIC)  # the recogniser is decoding this one right now
    system_job = decoder.take(Source.SYSTEM)
    assert in_flight.canary and decoder.pending

    started, release = threading.Event(), threading.Event()
    outcome = {}

    def decode_then_commit():
        started.set()
        release.wait(5)  # long decode
        outcome["mic"] = decoder.commit(in_flight)

    worker = threading.Thread(target=decode_then_commit)
    worker.start()
    started.wait(5)
    pending_mic_ns = sum(j.duration_ns for j in decoder.pending if j.source is Source.MIC)
    assert pending_mic_ns > 0
    transition = rig.exclude()
    assert not [j for j in decoder.pending if j.source is Source.MIC]  # pending mic jobs dropped
    release.set()
    worker.join(5)
    assert outcome["mic"] is False and decoder.rejected == [in_flight]
    assert decoder.commit(system_job) is True  # system results still commit
    assert all(not j.canary for j in decoder.committed)
    assert rig.controller.boundary.rejected_commits == 1
    [event] = [e for e in rig.controller.events() if isinstance(e, MicPrivacyStateEvent)]
    assert event.payload.discarded_pending_ms == pending_mic_ns // 1_000_000  # reported to the user
    assert transition.current.generation == 1


def test_unmute_starts_fresh_new_epoch_and_no_excluded_pre_roll():
    """Unmute opens a fresh epoch at sample 0 with no pre-roll from the muted interval."""
    rig = Rig()
    rig.start()
    rig.run(SECOND)
    rig.exclude()
    rig.run(3 * SECOND)
    rig.sink.resets.clear()
    rig.include()
    assert rig.sink.resets == [Source.MIC]  # downstream pre-roll/VAD state cleared again
    rig.run(4 * SECOND)
    mic = rig.sink.of(Source.MIC)
    after = [s for s in mic if s.epoch == 2]
    assert after and after[0].first_sample_index == 0 and after[0].monotonic_ns == 3 * SECOND
    assert {s.generation for s in after} == {2}
    assert not [s for s in mic if SECOND <= s.monotonic_ns < 3 * SECOND]
    assert rig.controller.status()["privacy_generation"] == 2


def test_exclusion_persists_through_pause_resume_switch_and_reconnect():
    """Mute persists through pause/resume, a device switch and a reconnect."""
    rig = Rig()
    rig.start()
    rig.run(SECOND)
    rig.exclude()
    opened = len(rig.backend.opened)
    rig.controller.pause()
    rig.controller.resume()
    rig.controller.switch_source(Source.MIC, MIC2, expected_epoch=rig.controller.source_epoch(Source.MIC))
    rig.controller.reconnect(Source.MIC)
    rig.run(3 * SECOND)
    assert not rig.controller.mic_included
    assert [h.source for h in rig.backend.opened[opened:]] == [Source.SYSTEM]  # only system reopened
    assert rig.controller.selected_device(Source.MIC) == MIC2
    assert not [s for s in rig.sink.of(Source.MIC) if s.monotonic_ns >= SECOND]
    rig.include()
    assert rig.handle(Source.MIC)._spec.device_id == MIC2  # unmute uses the new selection


def test_exclusion_persists_when_mic_is_lost_and_reconnected():
    rig = Rig()
    rig.start()
    rig.run(SECOND)
    rig.handle(Source.MIC).lose()
    rig.controller.check_health()
    rig.exclude()
    rig.controller.reconnect(Source.MIC)
    assert not rig.controller.mic_included
    assert rig.handle(Source.MIC) is None


def test_remote_or_stale_unmute_cannot_reinclude_and_repeats_are_noops():
    rig = Rig()
    rig.start()
    first = rig.exclude()
    assert rig.exclude().changed is False  # key repeat / double click
    assert rig.controller.privacy_generation == first.current.generation == 1
    with pytest.raises(LocalConfirmationRequired):
        rig.controller.set_mic_included(True, expected_generation=1, origin=CommandOrigin.REMOTE)
    with pytest.raises(StaleGenerationError):
        rig.controller.set_mic_included(True, expected_generation=0)
    assert not rig.controller.mic_included and rig.handle(Source.MIC) is None
    # A remote surface may still exclude immediately.
    rig.include()
    rig.controller.set_mic_included(False, expected_generation=2, origin=CommandOrigin.REMOTE)
    assert not rig.controller.mic_included


def test_starting_excluded_never_opens_the_microphone():
    rig = Rig(mic_included=False)
    result = rig.start()
    assert result.state is MeetingState.CAPTURING and result.opened == {Source.SYSTEM}
    rig.run(2 * SECOND)
    assert [h.source for h in rig.backend.opened] == [Source.SYSTEM]
    assert rig.sink.of(Source.MIC) == []


def test_every_meeting_requires_an_explicit_mic_choice():
    rig = Rig()
    with pytest.raises(TypeError):
        CaptureController(rig.backend)  # no default inclusion state


def test_stop_drain_never_reintroduces_excluded_audio():
    rig = Rig()
    rig.start()
    rig.handle(Source.MIC).signal = canary_signal
    rig.run(SECOND, process=False)
    rig.exclude()
    rig.controller.stop()
    assert_no_canary_downstream(rig)
    assert rig.sink.of(Source.SYSTEM)  # pending system audio was still drained


def test_concurrent_callbacks_worker_and_toggles_never_leak_canary():
    """Race test: a producer thread delivers mic buffers like a native callback (stamping
    the generation it reads at capture), a worker thread forwards, and the UI thread
    toggles. Audio captured after an exclusion is acknowledged is canary."""
    rig = Rig()
    controller = rig.controller
    rig.start(mic=MIC, system=None)
    acknowledged_excluded = threading.Event()
    stop = threading.Event()
    errors: list[BaseException] = []

    def producer():
        index = 0
        try:
            while not stop.is_set():
                # Stamp first, as a native callback does when its buffer completes; then
                # decide whether this buffer holds speech from after an acknowledged
                # exclusion. (Reading the flag first would let a buffer marked canary be
                # stamped by a later re-inclusion, i.e. audio captured after unmute.)
                generation = controller.privacy_generation
                excluded = acknowledged_excluded.is_set()
                frame = mic_frame(generation, index=index, value=CANARY if excluded else 0.01,
                                  epoch=max(1, controller.source_epoch(Source.MIC)))
                controller._on_frame(frame)
                index += 1
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    def worker():
        while not stop.is_set():
            controller.process_pending()

    threads = [threading.Thread(target=producer), threading.Thread(target=worker)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + 1.5
    toggles = 0
    while time.monotonic() < deadline:
        rig.exclude()
        acknowledged_excluded.set()
        time.sleep(0.002)
        acknowledged_excluded.clear()
        rig.include()
        toggles += 1
        time.sleep(0.002)
    stop.set()
    for t in threads:
        t.join(5)
    controller.process_pending()
    assert not errors
    assert toggles > 20
    assert rig.sink.seen, "the race test must actually forward included audio"
    assert controller.boundary.rejected_frames[Source.MIC] > 0, "the race must actually hit the gate"
    assert_no_canary_downstream(rig)
