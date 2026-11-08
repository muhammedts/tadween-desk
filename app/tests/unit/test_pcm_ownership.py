"""Frames and chunks own a tight, private copy of their PCM."""

from __future__ import annotations

from uuid import uuid4

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from meeting_minutes.audio.bounded_queue import BoundedFrameQueue
from meeting_minutes.domain.contracts import AudioChunk, AudioFrame, Source, TimingQuality

MEETING = uuid4()


def frame(data: np.ndarray, seq: int = 0) -> AudioFrame:
    return AudioFrame(
        meeting_id=MEETING, source=Source.MIC, epoch=1, privacy_generation=0, sequence=seq,
        first_sample_index=seq * data.shape[0], sample_rate=48_000, channels=data.shape[1],
        monotonic_ns=0, timing_quality=TimingQuality.SYNTHETIC, data=data,
    )


def chunk(data: np.ndarray) -> AudioChunk:
    return AudioChunk(
        meeting_id=MEETING, source=Source.MIC, epoch=1, privacy_generation=0,
        chunk_index=0, start_ms=0, end_ms=data.shape[0] // 16, data=data,
    )


def retained_bytes(array: np.ndarray) -> int:
    root = array
    while isinstance(root.base, np.ndarray):
        root = root.base
    return root.nbytes


def assert_owned(array: np.ndarray) -> None:
    assert array.base is None, "carrier must own its sample memory"
    assert array.flags.c_contiguous
    assert not array.flags.writeable


def test_producer_reuse_after_frame_creation_does_not_change_frame():
    producer = np.zeros((480, 1), dtype=np.float32)
    f = frame(producer)
    producer[:] = 0.5
    assert np.all(f.data == 0)
    assert producer.flags.writeable, "the producer's own buffer is left untouched"


def test_repeated_buffer_reuse_keeps_each_frame_distinct():
    producer = np.empty((480, 1), dtype=np.float32)
    q = BoundedFrameQueue(Source.MIC, max_bytes=10 * 480 * 4, max_duration_ns=10**9)
    for i in range(5):
        producer[:] = float(i)
        q.put(frame(producer, seq=i))
    producer[:] = -1.0
    values = [q.get().data[0, 0] for _ in range(5)]
    assert values == [0.0, 1.0, 2.0, 3.0, 4.0]


def test_small_view_of_large_buffer_retains_only_its_own_bytes():
    big = np.zeros((262_144, 1), dtype=np.float32)  # 1 MiB
    f = frame(big[1000:1480])
    assert_owned(f.data)
    assert retained_bytes(f.data) == f.nbytes == 480 * 4
    q = BoundedFrameQueue(Source.MIC, max_bytes=480 * 4, max_duration_ns=10**9)
    q.put(f)
    assert retained_bytes(q.get().data) <= q.max_bytes


def test_non_contiguous_and_fortran_inputs_are_copied_compactly():
    strided = np.arange(1920, dtype=np.float32).reshape(960, 2)[::2]  # every other row
    f = frame(strided)
    assert_owned(f.data)
    assert np.array_equal(f.data, strided)
    assert f.nbytes == 480 * 2 * 4

    fortran = np.asfortranarray(np.arange(960, dtype=np.float32).reshape(480, 2))
    g = frame(fortran)
    assert_owned(g.data)
    assert np.array_equal(g.data, fortran)


def test_chunk_owns_its_samples_too():
    big = np.zeros(160_000, dtype=np.float32)
    view = big[16_000:24_000]
    c = chunk(view)
    big[:] = 0.25
    assert np.all(c.data == 0)
    assert_owned(c.data)
    assert retained_bytes(c.data) == c.data.nbytes == 8_000 * 4

    strided = np.arange(16_000, dtype=np.float32)[::2]
    d = chunk(strided)
    assert_owned(d.data)
    assert np.array_equal(d.data, strided)


def test_dtype_is_still_validated_not_silently_converted():
    with pytest.raises(ValueError):
        frame(np.zeros((480, 1), dtype=np.float64))
    with pytest.raises(ValueError):
        chunk(np.zeros(8_000, dtype=np.int16))


@given(
    start=st.integers(min_value=0, max_value=2_000),
    length=st.integers(min_value=1, max_value=1_000),
    step=st.integers(min_value=1, max_value=4),
    channels=st.sampled_from([1, 2]),
)
def test_any_slice_becomes_an_exactly_sized_private_copy(start, length, step, channels):
    backing = np.random.default_rng(0).standard_normal((8_000, channels)).astype(np.float32)
    view = backing[start:start + length * step:step]
    f = frame(view)
    expected = view.copy()
    backing[:] = 0.0
    assert_owned(f.data)
    assert np.array_equal(f.data, expected)
    assert retained_bytes(f.data) == f.nbytes == view.shape[0] * channels * 4
