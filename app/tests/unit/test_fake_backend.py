from __future__ import annotations

from uuid import UUID

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from meeting_minutes.capture.fake import DEFAULT_FAKE_DEVICES, FakeAudioBackend, FakeDeviceSpec, pump_until
from meeting_minutes.domain.contracts import AudioFormat, AudioFrame, DeviceSelection, Direction, Source
from meeting_minutes.domain.privacy import MicPrivacyState, set_mic_included

MEETING = UUID("daef6350-149b-4b1f-a933-5ebd3a1c95d2")
MIC = DeviceSelection(source=Source.MIC, device_id="fake:mic-48k-mono")
SYSTEM = DeviceSelection(source=Source.SYSTEM, device_id="fake:system-44k1-stereo")


def run_two_sources(seconds: float, origin_ns: int = 5_000_000_000) -> list[AudioFrame]:
    frames: list[AudioFrame] = []
    backend = FakeAudioBackend(meeting_id=MEETING, origin_ns=origin_ns)
    handles = [backend.open(MIC, frames.append), backend.open(SYSTEM, frames.append)]
    pump_until(handles, origin_ns + int(seconds * 1e9))
    return frames


def signature(frames: list[AudioFrame]) -> list[tuple]:
    return [
        (f.source, f.sequence, f.first_sample_index, f.monotonic_ns, f.data.tobytes())
        for f in frames
    ]


def test_enumerates_one_capture_and_one_loopback_device():
    devices = FakeAudioBackend(meeting_id=MEETING).enumerate()
    assert [(d.direction, d.supports_loopback) for d in devices] == [
        (Direction.CAPTURE, False), (Direction.RENDER, True),
    ]
    assert all(d.backend == "fake" for d in devices)


def test_two_source_output_is_deterministic():
    assert signature(run_two_sources(1.0)) == signature(run_two_sources(1.0))


def test_counters_are_contiguous_and_timestamps_exact():
    origin = 5_000_000_000
    frames = run_two_sources(1.0, origin)
    for source, rate, channels in ((Source.MIC, 48_000, 1), (Source.SYSTEM, 44_100, 2)):
        own = [f for f in frames if f.source is source]
        assert [f.sequence for f in own] == list(range(len(own)))
        assert own[0].first_sample_index == 0
        for prev, cur in zip(own, own[1:]):
            assert cur.first_sample_index == prev.end_sample_index
        for f in own:
            assert f.sample_rate == rate and f.channels == channels
            assert f.monotonic_ns == origin + f.first_sample_index * 1_000_000_000 // rate
        # Exactly one second of samples per source.
        assert own[-1].end_sample_index == rate


def test_frames_are_delivered_in_capture_time_order():
    frames = run_two_sources(0.5)
    times = [f.monotonic_ns for f in frames]
    assert times == sorted(times)
    assert {f.source for f in frames} == {Source.MIC, Source.SYSTEM}


def test_sources_are_distinguishable_signals():
    frames = run_two_sources(0.1)
    mic = next(f for f in frames if f.source is Source.MIC)
    system = next(f for f in frames if f.source is Source.SYSTEM)
    assert not np.array_equal(system.data[:, 0], system.data[:, 1])
    assert not np.allclose(mic.data[:441, 0], system.data[:, 0])


def test_output_endpoint_cannot_be_opened_as_mic_and_vice_versa():
    backend = FakeAudioBackend(meeting_id=MEETING)
    with pytest.raises(ValueError):
        backend.open(DeviceSelection(source=Source.MIC, device_id="fake:system-44k1-stereo"), lambda f: None)
    with pytest.raises(ValueError):
        backend.open(DeviceSelection(source=Source.SYSTEM, device_id="fake:mic-48k-mono"), lambda f: None)
    with pytest.raises(LookupError):
        backend.open(DeviceSelection(source=Source.MIC, device_id="fake:missing"), lambda f: None)


def test_stop_is_idempotent_and_ends_delivery():
    frames: list[AudioFrame] = []
    handle = FakeAudioBackend(meeting_id=MEETING).open(MIC, frames.append)
    assert handle.pump(2) == 2
    handle.stop()
    handle.close()
    handle.close()
    assert handle.pump(5) == 0
    assert len(frames) == 2


def test_frames_carry_current_privacy_generation_and_mute_survives_device_switch():
    privacy = {"state": MicPrivacyState.initial(included=True)}
    devices = (*DEFAULT_FAKE_DEVICES, FakeDeviceSpec(
        device_id="fake:usb-mic-16k", display_name="Fake USB mic", direction=Direction.CAPTURE,
        fmt=AudioFormat(sample_rate=16_000, channels=1), tone_hz=300.0, frames_per_buffer=160,
    ))
    backend = FakeAudioBackend(
        meeting_id=MEETING, devices=devices,
        privacy_generation=lambda source: privacy["state"].generation,
    )
    frames: list[AudioFrame] = []
    old = backend.open(MIC, frames.append, epoch=1)
    old.pump(1)
    privacy["state"] = set_mic_included(privacy["state"], False, expected_generation=0).current
    old.close()
    new = backend.open(DeviceSelection(source=Source.MIC, device_id="fake:usb-mic-16k"), frames.append, epoch=2)
    new.pump(1)

    before, after = frames
    assert (before.epoch, before.privacy_generation) == (1, 0)
    assert (after.epoch, after.privacy_generation) == (2, 1)
    assert not privacy["state"].mic_included  # the switch did not unmute
    assert not privacy["state"].admits(Source.MIC, before.privacy_generation)
    assert not privacy["state"].admits(Source.MIC, after.privacy_generation)


@given(
    rate=st.sampled_from([8_000, 16_000, 22_050, 44_100, 48_000, 96_000]),
    block=st.integers(min_value=1, max_value=2_048),
    buffers=st.integers(min_value=1, max_value=40),
    origin=st.integers(min_value=0, max_value=10**15),
)
def test_sample_accounting_property(rate, block, buffers, origin):
    spec = FakeDeviceSpec(
        device_id="fake:any", display_name="any", direction=Direction.CAPTURE,
        fmt=AudioFormat(sample_rate=rate, channels=1), tone_hz=100.0, frames_per_buffer=block,
    )
    frames: list[AudioFrame] = []
    handle = FakeAudioBackend(meeting_id=MEETING, devices=[spec], origin_ns=origin).open(
        DeviceSelection(source=Source.MIC, device_id="fake:any"), frames.append
    )
    handle.pump(buffers)
    assert sum(f.frame_count for f in frames) == block * buffers
    assert frames[-1].end_sample_index == block * buffers
    assert all(f.monotonic_ns == origin + f.first_sample_index * 10**9 // rate for f in frames)
    assert all(a.monotonic_ns <= b.monotonic_ns for a, b in zip(frames, frames[1:]))
