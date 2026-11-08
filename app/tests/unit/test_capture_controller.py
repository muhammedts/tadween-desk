"""Capture controller: lifecycle, epochs, gaps, bounded memory (synthetic devices)."""

from __future__ import annotations

import json

import pytest
from capture_rig import MIC, MIC2, SECOND, SYSTEM, Rig

from meeting_minutes.capture.controller import CaptureConfig, CaptureError, StaleEpochError
from meeting_minutes.domain.contracts import Source
from meeting_minutes.domain.events import GapReason, parse_event
from meeting_minutes.domain.states import MeetingState, SourceState


def gaps(rig, reason=None, source=None):
    return [g for g in rig.controller.gaps
            if (reason is None or g.reason is reason) and (source is None or g.source is source)]


# ------------------------------------------------------------------ start / stop


def test_nothing_opens_before_an_explicit_start():
    rig = Rig()
    rig.exclude()
    rig.include()
    assert rig.backend.opened == []
    assert rig.controller.state is MeetingState.IDLE
    result = rig.start()
    assert result.state is MeetingState.CAPTURING and result.opened == {Source.MIC, Source.SYSTEM}
    assert {h.source for h in rig.backend.opened} == {Source.MIC, Source.SYSTEM}


def test_start_requires_a_selection_and_cannot_repeat():
    rig = Rig()
    with pytest.raises(ValueError):
        rig.start(mic=None, system=None)
    rig.start()
    with pytest.raises(CaptureError):
        rig.start()


def test_failed_source_rolls_back_unless_degraded_start_is_chosen():
    rig = Rig()
    rig.backend.fail_open.add(SYSTEM)
    result = rig.start()
    assert not result.started and result.failed == {Source.SYSTEM: "device_unavailable"}
    assert rig.controller.state is MeetingState.PREFLIGHT
    assert all(h.closed for h in rig.backend.opened)  # the mic that did open was closed again
    result = rig.start(allow_degraded=True)
    assert result.state is MeetingState.DEGRADED and result.opened == {Source.MIC}
    assert rig.controller.source_state(Source.SYSTEM) is SourceState.FAILED
    # No silent substitute: only the selected endpoints were ever opened.
    assert {h._spec.device_id for h in rig.backend.opened} == {MIC}


def test_stop_is_idempotent_releases_streams_and_buffers():
    rig = Rig()
    rig.start()
    rig.run(SECOND, process=False)
    assert rig.controller.pcm_bytes() > 0
    first = rig.controller.stop()
    assert first.drain_completed and first.drained_frames > 0 and first.undrained_frames == 0
    assert rig.controller.stop().drained_frames == 0
    assert rig.controller.state is MeetingState.STOPPED
    assert all(h.closed for h in rig.backend.opened)
    assert rig.controller.pcm_bytes() == 0
    assert all(e.end_ms is not None for e in rig.controller.epochs)
    with pytest.raises(CaptureError):
        rig.controller.switch_source(Source.MIC, MIC2, expected_epoch=1)


def test_stop_before_start_opens_nothing():
    rig = Rig()
    assert rig.controller.stop().drain_completed
    assert rig.backend.opened == [] and rig.controller.state is MeetingState.STOPPED


def test_drain_budget_exhausted_marks_undrained_audio_as_gaps():
    rig = Rig(config=CaptureConfig(mic_stall_timeout_ns=None, drain_timeout_ns=0))
    rig.start()
    rig.run(SECOND, process=False)
    result = rig.controller.stop()
    assert not result.drain_completed and result.undrained_frames > 0
    undrained = gaps(rig, GapReason.STOPPED_UNDRAINED)
    assert {g.source for g in undrained} == {Source.MIC, Source.SYSTEM}
    assert sum(g.end_ms - g.start_ms for g in undrained if g.source is Source.MIC) >= 990


# ------------------------------------------------------------- sources and time


def test_two_sources_keep_their_own_rate_epoch_and_timeline():
    """48 kHz mono and 44.1 kHz stereo, distinct and in capture-time order."""
    rig = Rig()
    rig.now = 7 * SECOND
    rig.start()
    rig.run(9 * SECOND)
    mic, system = rig.sink.of(Source.MIC), rig.sink.of(Source.SYSTEM)
    assert len(mic) == 200 and len(system) == 200
    assert mic[-1].first_sample_index == 199 * 480 and system[-1].first_sample_index == 199 * 441
    assert mic[0].monotonic_ns == system[0].monotonic_ns == 7 * SECOND
    times = [s.monotonic_ns for s in rig.sink.seen]
    assert times == sorted(times)  # forwarded oldest capture time first across sources
    status = rig.controller.status()
    assert status["sources"]["mic"]["samples_admitted"] == 2 * 48_000
    assert status["sources"]["system"]["samples_admitted"] == 2 * 44_100
    assert gaps(rig) == []


def test_no_data_interval_becomes_an_explicit_gap():
    rig = Rig()
    rig.start()
    rig.run(SECOND)
    rig.handle(Source.SYSTEM).skip(2 * 44_100)  # loopback idle: no packets for 2 s
    rig.run(4 * SECOND)
    [gap] = gaps(rig, GapReason.NO_DATA_DELIVERED)
    assert gap.source is Source.SYSTEM and (gap.start_ms, gap.end_ms) == (1000, 3000)
    assert gaps(rig, source=Source.MIC) == []


def test_switching_one_source_keeps_the_other_running_with_exact_gap():
    rig = Rig()
    rig.start()
    rig.run(SECOND)
    system_handle = rig.handle(Source.SYSTEM)
    open_device = rig.backend.open

    def slow_open(*args, **kwargs):
        rig.now += 300_000_000  # opening the new endpoint takes 300 ms
        return open_device(*args, **kwargs)

    rig.backend.open = slow_open
    state = rig.controller.switch_source(Source.MIC, MIC2, expected_epoch=1).state
    assert state is SourceState.RUNNING and rig.controller.source_epoch(Source.MIC) == 2
    assert rig.handle(Source.SYSTEM) is system_handle and system_handle.running
    rig.run(3 * SECOND)
    [gap] = gaps(rig, GapReason.DEVICE_SWITCH)
    assert gap.source is Source.MIC and gap.epoch == 2 and (gap.start_ms, gap.end_ms) == (1000, 1300)
    epochs = [(e.source, e.epoch, e.native_rate) for e in rig.controller.epochs]
    assert (Source.MIC, 2, 16_000) in epochs
    system = rig.sink.of(Source.SYSTEM)
    assert {s.epoch for s in system} == {1} and len(system) == 300


def test_stale_switch_command_is_rejected():
    rig = Rig()
    rig.start()
    rig.controller.switch_source(Source.MIC, MIC2, expected_epoch=1)
    with pytest.raises(StaleEpochError):
        rig.controller.switch_source(Source.MIC, MIC, expected_epoch=1)


def test_failed_switch_reopens_previous_device_once():
    rig = Rig()
    rig.start()
    rig.backend.fail_open.add(MIC2)
    state = rig.controller.switch_source(Source.MIC, MIC2, expected_epoch=1).state
    assert state is SourceState.RUNNING and rig.controller.selected_device(Source.MIC) == MIC
    assert rig.controller.state is MeetingState.CAPTURING
    rig.backend.fail_open.update({MIC, MIC2})
    state = rig.controller.switch_source(Source.MIC, MIC2, expected_epoch=rig.controller.source_epoch(Source.MIC)).state
    assert state is SourceState.FAILED and rig.controller.state is MeetingState.DEGRADED


def test_lost_device_degrades_other_continues_and_reconnect_records_gap():
    """Unplug -> explicit lost state, no fallback; reconnect opens a new epoch."""
    rig = Rig()
    rig.start()
    rig.run(SECOND)
    rig.handle(Source.MIC).lose()
    rig.controller.check_health()
    assert rig.controller.source_state(Source.MIC) is SourceState.LOST
    assert rig.controller.state is MeetingState.DEGRADED
    rig.run(3 * SECOND)
    assert rig.sink.of(Source.SYSTEM)[-1].monotonic_ns > 2 * SECOND
    assert len(rig.backend.opened) == 2  # nothing else was opened in its place
    assert rig.controller.reconnect(Source.MIC).state is SourceState.RUNNING
    assert rig.controller.state is MeetingState.CAPTURING
    rig.run(4 * SECOND)
    [gap] = gaps(rig, GapReason.DEVICE_LOST)
    assert gap.source is Source.MIC and (gap.start_ms, gap.end_ms) == (1000, 3000)


def test_unmute_after_a_long_mute_is_not_mistaken_for_a_lost_mic():
    """A reopened mic is judged from its new stream, not from the last audio
    admitted before a mute longer than the stall timeout."""
    rig = Rig(config=CaptureConfig(mic_stall_timeout_ns=3 * SECOND))
    rig.start()
    rig.run(SECOND)
    rig.exclude()
    rig.run(6 * SECOND)  # muted for 5 s: no mic audio admitted
    rig.include()
    rig.controller.check_health()  # before the reopened stream's first buffer
    assert rig.controller.source_state(Source.MIC) is SourceState.RUNNING
    rig.run(7 * SECOND)
    assert rig.controller.source_state(Source.MIC) is SourceState.RUNNING
    rig.handle(Source.MIC).stop()  # a mic that really stops delivering is still detected
    rig.handle(Source.MIC)._active = True
    rig.now = 10 * SECOND + 1
    rig.controller.check_health()
    assert rig.controller.source_state(Source.MIC) is SourceState.LOST


def test_silent_microphone_is_detected_as_lost():
    rig = Rig(config=CaptureConfig(mic_stall_timeout_ns=3 * SECOND))
    rig.start()
    rig.run(SECOND)
    rig.handle(Source.MIC).stop()  # stream claims to be active but delivers nothing
    rig.handle(Source.MIC)._active = True
    rig.now = 3 * SECOND
    rig.controller.check_health()
    assert rig.controller.source_state(Source.MIC) is SourceState.RUNNING
    rig.now = 4 * SECOND + 1
    rig.controller.check_health()
    assert rig.controller.source_state(Source.MIC) is SourceState.LOST
    assert rig.controller.source_state(Source.SYSTEM) is SourceState.RUNNING  # loopback may be idle


def test_pause_all_closes_both_and_resume_opens_new_epochs_without_loss_gaps():
    rig = Rig()
    rig.start()
    rig.run(SECOND)
    assert rig.controller.pause()
    assert all(h.closed for h in rig.backend.opened)
    assert rig.controller.state is MeetingState.PAUSED
    rig.run(3 * SECOND)
    assert rig.controller.resume()
    rig.run(4 * SECOND)
    assert rig.controller.source_epoch(Source.MIC) == 2 and rig.controller.source_epoch(Source.SYSTEM) == 2
    assert gaps(rig) == []  # pause is intentional, recorded as state events
    seen = [s.monotonic_ns for s in rig.sink.seen]
    assert not [t for t in seen if SECOND <= t < 3 * SECOND]


# ------------------------------------------------------------- bounded memory


def test_backlog_beyond_24_seconds_drops_oldest_with_exact_gap():
    """Processing stalls; the queue stays bounded and the loss is explicit."""
    rig = Rig()
    rig.start(mic=MIC, system=None)
    rig.run(30 * SECOND, process=False)
    status = rig.controller.status()
    assert status["sources"]["mic"]["queued_ms"] <= 24_000
    rig.controller.process_pending()
    overflow = gaps(rig, GapReason.QUEUE_OVERFLOW)
    assert sum(g.end_ms - g.start_ms for g in overflow) == 6000
    assert overflow[0].start_ms == 0
    assert len(rig.sink.seen) * 10 == 24_000


def test_global_pcm_budget_is_never_exceeded_and_losses_are_recorded():
    budget = 256 * 1024
    rig = Rig(config=CaptureConfig(mic_stall_timeout_ns=None, max_pcm_bytes_total=budget))
    rig.start()
    rig.run(5 * SECOND, process=False)
    assert rig.controller.peak_pcm_bytes <= budget
    assert rig.controller.pcm_bytes() <= budget
    rig.controller.process_pending()
    budget_gaps = gaps(rig, GapReason.PCM_BUDGET)
    assert budget_gaps and {g.source for g in budget_gaps} == {Source.MIC, Source.SYSTEM}


def test_default_budget_is_64_mib_and_two_sources_stay_well_within_it():
    rig = Rig()
    assert rig.controller.status()["pcm_budget_bytes"] == 64 * 2**20
    rig.start()
    rig.run(24 * SECOND, process=False)
    # 24 s of 48 kHz mono + 44.1 kHz stereo float32 ~= 13.1 MB of owned PCM.
    assert rig.controller.peak_pcm_bytes == 24 * (48_000 * 4 + 44_100 * 2 * 4)
    assert gaps(rig) == []


def test_sink_failure_is_counted_and_capture_continues():
    class Exploding:
        controller = None

        def accept(self, frame):
            raise RuntimeError("downstream bug")

        def reset(self, source):
            return 0

    rig = Rig(sink=Exploding())
    rig.start()
    rig.run(SECOND)
    assert rig.controller.sink_errors > 0
    assert rig.controller.state is MeetingState.CAPTURING
    assert rig.controller.pcm_bytes() == 0  # failed frames were still released


# ----------------------------------------------------------------------- events


def test_events_are_metadata_only_and_round_trip():
    rig = Rig()
    rig.start()
    rig.run(SECOND)
    rig.exclude()
    rig.controller.switch_source(Source.SYSTEM, SYSTEM, expected_epoch=1)
    rig.controller.pause()
    rig.controller.resume()
    rig.controller.stop()
    events = rig.controller.events()
    assert [e.stream_seq for e in events] == list(range(len(events)))
    types = {e.type for e in events}
    assert types >= {"capture.state", "source.changed", "mic.privacy_state"}
    for event in events:
        raw = event.model_dump_json()
        assert parse_event(raw) == event
        assert set(json.loads(raw)["payload"]) <= {
            "source", "state", "epoch", "reason", "at_ms", "start_ms", "end_ms", "code",
            "privacy_generation", "discarded_pending_ms", "device_id", "sample_rate", "channels", "duration_known",
        }


def test_status_snapshot_is_json_and_content_free():
    rig = Rig()
    rig.start()
    rig.run(SECOND)
    status = rig.controller.status()
    rendered = json.dumps(status)
    assert status["sources"]["mic"]["frames_forwarded"] == 100
    assert "data" not in rendered and "samples\"" not in rendered


def test_callback_in_flight_during_close_leaves_no_owned_pcm():
    """A native callback that completes after its stream closed must not queue audio."""
    rig = Rig()
    rig.start()
    rig.run(SECOND)
    late_system = rig.handle(Source.SYSTEM)
    rig.controller.stop()
    late_system._running = True  # the PortAudio thread was mid-callback during close
    late_system.pump(1)
    assert rig.controller.pcm_bytes() == 0
    assert rig.controller.status()["frames_after_close"] == 1


def test_loss_gap_ends_where_pause_or_mic_exclusion_begins():
    rig = Rig()
    rig.start()
    rig.run(SECOND)
    rig.handle(Source.MIC).lose()
    rig.handle(Source.SYSTEM).lose()
    rig.controller.check_health()
    rig.run(2 * SECOND)
    rig.exclude()  # the mic's absence is intentional from here
    rig.run(3 * SECOND)
    rig.controller.pause()  # and the system's from here
    rig.run(5 * SECOND)
    rig.controller.resume()
    rig.run(6 * SECOND)
    lost = {g.source: (g.start_ms, g.end_ms) for g in gaps(rig, GapReason.DEVICE_LOST)}
    assert lost == {Source.MIC: (1000, 2000), Source.SYSTEM: (1000, 3000)}


def test_switching_a_lost_source_keeps_the_loss_reason():
    rig = Rig()
    rig.start()
    rig.run(SECOND)
    rig.handle(Source.MIC).lose()
    rig.controller.check_health()
    rig.run(2 * SECOND)
    rig.controller.switch_source(Source.MIC, MIC2, expected_epoch=1)
    rig.run(3 * SECOND)
    assert [(g.reason, g.start_ms, g.end_ms) for g in gaps(rig, source=Source.MIC)] == [
        (GapReason.DEVICE_LOST, 1000, 2000)
    ]


def test_idle_loopback_at_epoch_edges_is_recorded_as_no_data():
    """Observed on real hardware: WASAPI loopback delivers nothing until sound plays and stops
    delivering after it ends; both edges are explicit, start-up latency is not."""
    rig = Rig()
    rig.start()
    system = rig.handle(Source.SYSTEM)
    system.stop()  # loopback idle: no buffers
    rig.run(2 * SECOND)
    system._running = True  # sound starts at 2 s
    system.skip(2 * 44_100)
    rig.run(4 * SECOND)
    system.stop()  # sound ends at 4 s
    rig.run(5 * SECOND)
    rig.controller.pause()
    no_data = [(g.start_ms, g.end_ms) for g in gaps(rig, GapReason.NO_DATA_DELIVERED, Source.SYSTEM)]
    assert no_data == [(0, 2000), (4000, 5000)]
    assert gaps(rig, source=Source.MIC) == []


def test_switch_onto_idle_loopback_separates_switch_gap_from_no_data():
    rig = Rig()
    rig.start()
    rig.run(SECOND)
    rig.now += 50_000_000  # 50 ms to reopen
    rig.controller.switch_source(Source.SYSTEM, rig.controller.selected_device(Source.SYSTEM), expected_epoch=1)
    new = rig.handle(Source.SYSTEM)
    new.stop()
    rig.run(3 * SECOND)  # the new endpoint is open but idle
    new._running = True
    new.skip((3 * SECOND - 1_050_000_000) * 44_100 // SECOND)
    rig.run(4 * SECOND)
    system = [(g.reason, g.start_ms, g.end_ms) for g in gaps(rig, source=Source.SYSTEM)]
    assert system == [(GapReason.DEVICE_SWITCH, 1000, 1050), (GapReason.NO_DATA_DELIVERED, 1050, 3000)]
