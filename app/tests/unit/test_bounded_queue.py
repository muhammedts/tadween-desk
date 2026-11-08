from __future__ import annotations

import threading
from dataclasses import replace
from uuid import uuid4

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from meeting_minutes.audio.bounded_queue import BoundedFrameQueue
from meeting_minutes.capture.fake import DEFAULT_FAKE_DEVICES, FakeAudioBackend, pump_until
from meeting_minutes.domain.contracts import AudioFrame, DeviceSelection, Source, TimingQuality

MEETING = uuid4()


def frame(seq: int, count: int = 480, source: Source = Source.MIC) -> AudioFrame:
    return AudioFrame(
        meeting_id=MEETING, source=source, epoch=1, privacy_generation=0, sequence=seq,
        first_sample_index=seq * count, sample_rate=48_000, channels=1,
        monotonic_ns=seq * count * 10**9 // 48_000, timing_quality=TimingQuality.SYNTHETIC,
        data=np.zeros((count, 1), dtype=np.float32),
    )


def test_drops_oldest_whole_frames_and_reports_exact_spans():
    q = BoundedFrameQueue(Source.MIC, max_bytes=3 * 480 * 4, max_duration_ns=10**9)
    for i in range(3):
        assert q.put(frame(i)) == []
    dropped = q.put(frame(3))
    assert [(d.first_sample_index, d.end_sample_index) for d in dropped] == [(0, 480)]
    assert len(q) == 3 and q.dropped_frames == 1 and q.dropped_samples == 480
    assert q.get().sequence == 1


def test_duration_cap_applies_independently_of_bytes():
    q = BoundedFrameQueue(Source.MIC, max_bytes=10**9, max_duration_ns=25_000_000)  # 25 ms
    for i in range(4):
        q.put(frame(i))  # 10 ms each
    assert len(q) == 2
    assert q.queued_duration_ns <= 25_000_000


def test_oversized_frame_and_wrong_source_rejected():
    q = BoundedFrameQueue(Source.MIC, max_bytes=100, max_duration_ns=10**9)
    with pytest.raises(ValueError):
        q.put(frame(0))
    q = BoundedFrameQueue(Source.MIC, max_bytes=10**6, max_duration_ns=10**9)
    with pytest.raises(ValueError):
        q.put(frame(0, source=Source.SYSTEM))


def test_clear_releases_everything():
    q = BoundedFrameQueue(Source.MIC, max_bytes=10**6, max_duration_ns=10**9)
    for i in range(5):
        q.put(frame(i))
    assert q.clear() == 5
    assert len(q) == 0 and q.queued_bytes == 0 and q.queued_duration_ns == 0
    assert q.get() is None


def test_fake_two_hour_stream_stays_bounded():
    """Two simulated hours per source through 24-second queues never exceed the caps."""
    caps = {}
    queues = {}
    for source, rate, channels in ((Source.MIC, 48_000, 1), (Source.SYSTEM, 44_100, 2)):
        max_bytes = 24 * rate * channels * 4
        queues[source] = BoundedFrameQueue(source, max_bytes=max_bytes, max_duration_ns=24 * 10**9)
        caps[source] = max_bytes
    peak = {s: 0 for s in queues}
    dropped = {s: 0 for s in queues}

    def sink(f: AudioFrame) -> None:
        q = queues[f.source]
        dropped[f.source] += sum(d.end_sample_index - d.first_sample_index for d in q.put(f))
        peak[f.source] = max(peak[f.source], q.queued_bytes)

    # 100 ms buffers keep two simulated hours fast; caps and accounting are unchanged.
    devices = [replace(d, frames_per_buffer=d.fmt.sample_rate // 10) for d in DEFAULT_FAKE_DEVICES]
    backend = FakeAudioBackend(meeting_id=MEETING, devices=devices)
    handles = [
        backend.open(DeviceSelection(source=Source.MIC, device_id="fake:mic-48k-mono"), sink),
        backend.open(DeviceSelection(source=Source.SYSTEM, device_id="fake:system-44k1-stereo"), sink),
    ]
    pump_until(handles, 2 * 3600 * 10**9)

    for source, rate in ((Source.MIC, 48_000), (Source.SYSTEM, 44_100)):
        q = queues[source]
        assert peak[source] <= caps[source]
        assert q.queued_duration_ns <= 24 * 10**9
        # Nothing consumed, so queued + dropped samples account for every generated sample.
        queued_samples = q.queued_duration_ns * rate // 10**9
        assert dropped[source] + queued_samples == 2 * 3600 * rate


def test_concurrent_producers_respect_cap():
    q = BoundedFrameQueue(Source.MIC, max_bytes=10 * 480 * 4, max_duration_ns=10**12)

    def produce(offset: int) -> None:
        for i in range(500):
            q.put(frame(offset + i))

    threads = [threading.Thread(target=produce, args=(k * 1000,)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(q) == 10
    assert q.dropped_frames == 4 * 500 - 10


@given(st.lists(st.integers(min_value=1, max_value=4_800), min_size=1, max_size=200),
       st.integers(min_value=4_800 * 4, max_value=200_000))
def test_byte_cap_property(sizes, cap):
    q = BoundedFrameQueue(Source.MIC, max_bytes=cap, max_duration_ns=10**12)
    total_dropped = 0
    for i, n in enumerate(sizes):
        total_dropped += sum(d.end_sample_index - d.first_sample_index for d in q.put(frame(i, n)))
        assert q.queued_bytes <= cap
    assert total_dropped + q.queued_bytes // 4 == sum(sizes)
