from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from meeting_minutes.capture.clock import DRIFT_FLAG_NS, MIN_DISCONTINUITY_NS, SampleClock

RATE = 48_000
BUF = 480  # 10 ms
BUF_NS = 10_000_000


def test_first_buffer_sets_origin_at_its_first_sample():
    clock = SampleClock(RATE, frames_per_buffer=BUF)
    p = clock.place(BUF, observed_ns=5_000_000_000)
    assert p.first_sample_index == 0
    assert p.monotonic_ns == 5_000_000_000 - BUF_NS
    assert p.skipped_samples == 0


def test_regular_callbacks_map_linearly_despite_jitter():
    clock = SampleClock(RATE, frames_per_buffer=BUF)
    origin_observed = 1_000_000_000
    jitter = [0, 3_000_000, -2_000_000, 7_000_000, 0, 1_000_000]
    for i, j in enumerate(jitter):
        p = clock.place(BUF, origin_observed + i * BUF_NS + j)
        assert p.first_sample_index == i * BUF
        assert p.monotonic_ns == clock.origin_ns + i * BUF_NS
        assert p.skipped_samples == 0


def test_no_data_interval_is_skipped_not_compressed():
    """WASAPI loopback delivers nothing while nothing plays; time must still advance."""
    clock = SampleClock(RATE, frames_per_buffer=BUF)
    clock.place(BUF, 1_000_000_000)
    silence_ns = 3 * 1_000_000_000
    p = clock.place(BUF, 1_000_000_000 + BUF_NS + silence_ns)
    assert p.skipped_samples == silence_ns * RATE // 1_000_000_000
    assert p.first_sample_index == BUF + p.skipped_samples
    # The buffer is placed where it was actually captured (just before observation).
    assert abs(p.monotonic_ns + BUF_NS - (1_000_000_000 + BUF_NS + silence_ns)) <= 1_000


def test_late_callbacks_within_tolerance_do_not_create_gaps():
    clock = SampleClock(RATE, frames_per_buffer=BUF)
    clock.place(BUF, 1_000_000_000)
    p = clock.place(BUF, 1_000_000_000 + BUF_NS + MIN_DISCONTINUITY_NS - 1_000_000)
    assert p.skipped_samples == 0


def test_device_running_ahead_is_flagged_never_moved_backwards():
    clock = SampleClock(RATE, frames_per_buffer=BUF)
    observed = 1_000_000_000
    clock.place(BUF, observed)
    last = None
    # Deliver 100 buffers (1 s of samples) in only 0.9 s of monotonic time.
    for i in range(1, 101):
        p = clock.place(BUF, observed + i * 9_000_000)
        if last is not None:
            assert p.monotonic_ns > last
        last = p.monotonic_ns
    assert clock.max_device_lead_ns > DRIFT_FLAG_NS
    assert p.device_lead_ns > DRIFT_FLAG_NS


@given(st.lists(st.tuples(st.integers(1, 2048), st.integers(0, 2_000_000_000)), min_size=1, max_size=60))
def test_indices_are_contiguous_or_explicitly_skipped(buffers):
    clock = SampleClock(44_100, frames_per_buffer=441)
    observed = 10_000_000_000
    expected_next = 0
    for count, delay in buffers:
        observed += delay
        p = clock.place(count, observed)
        assert p.first_sample_index == expected_next + p.skipped_samples
        assert p.skipped_samples >= 0
        expected_next = p.first_sample_index + count


def test_rejects_invalid_arguments():
    with pytest.raises(ValueError):
        SampleClock(0, frames_per_buffer=1)
    with pytest.raises(ValueError):
        SampleClock(RATE, frames_per_buffer=BUF).place(0, 1)
