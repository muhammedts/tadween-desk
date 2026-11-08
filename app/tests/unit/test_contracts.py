from __future__ import annotations

import json
from uuid import uuid4

import numpy as np
import pytest
from pydantic import ValidationError

from meeting_minutes.domain.contracts import (
    AudioChunk,
    AudioFormat,
    AudioFrame,
    DeviceDescriptor,
    Direction,
    IdentityStability,
    Source,
    TimingQuality,
)


def make_frame(**overrides) -> AudioFrame:
    values = dict(
        meeting_id=uuid4(),
        source=Source.MIC,
        epoch=1,
        privacy_generation=0,
        sequence=0,
        first_sample_index=0,
        sample_rate=48_000,
        channels=1,
        monotonic_ns=0,
        timing_quality=TimingQuality.SYNTHETIC,
        data=np.full((480, 1), 0.123456, dtype=np.float32),
    )
    values.update(overrides)
    return AudioFrame(**values)


def descriptor(**overrides) -> DeviceDescriptor:
    values = dict(
        device_id="{0.0.1.00000000}.{opaque}",
        display_name="Headset microphone",
        backend="wasapi",
        direction=Direction.CAPTURE,
        identity_stability=IdentityStability.STABLE_ENDPOINT_ID,
        supports_loopback=False,
        probed_formats=(AudioFormat(sample_rate=48_000, channels=2),),
    )
    values.update(overrides)
    return DeviceDescriptor(**values)


def test_device_descriptor_round_trips_json():
    d = descriptor()
    assert DeviceDescriptor.model_validate_json(d.model_dump_json()) == d


@pytest.mark.parametrize("bad_id", ["3", " 12 ", "0"])
def test_numeric_enumeration_index_is_not_an_identity(bad_id):
    with pytest.raises(ValidationError):
        descriptor(device_id=bad_id)


def test_descriptor_rejects_unknown_fields_such_as_serials():
    with pytest.raises(ValidationError):
        DeviceDescriptor.model_validate({**descriptor().model_dump(), "serial_number": "X"})


def test_frame_derived_counters():
    f = make_frame(first_sample_index=960, data=np.zeros((480, 1), dtype=np.float32))
    assert f.frame_count == 480
    assert f.end_sample_index == 1440
    assert f.duration_ns == 10_000_000
    assert f.nbytes == 480 * 4


def test_frame_data_is_read_only():
    f = make_frame()
    with pytest.raises(ValueError):
        f.data[0, 0] = 1.0


def test_frame_repr_never_contains_samples():
    f = make_frame()
    assert "0.123" not in repr(f)
    assert "data" not in repr(f)


def test_frame_is_not_json_serialisable():
    with pytest.raises(TypeError):
        json.dumps(make_frame())


@pytest.mark.parametrize(
    "overrides",
    [
        {"data": np.zeros((480, 2), dtype=np.float32)},  # channel mismatch
        {"data": np.zeros((480, 1), dtype=np.float64)},  # dtype
        {"data": np.zeros((0, 1), dtype=np.float32)},  # empty
        {"sequence": -1},
        {"privacy_generation": -1},
        {"epoch": 0},  # epochs are one-based (source_epochs.epoch >= 1)
        {"sample_rate": 1000},
    ],
)
def test_frame_rejects_invalid_values(overrides):
    with pytest.raises(ValueError):
        make_frame(**overrides)


def test_chunk_checks_duration_and_lineage():
    chunk = AudioChunk(
        meeting_id=uuid4(), source=Source.SYSTEM, epoch=1, privacy_generation=0,
        chunk_index=0, start_ms=1000, end_ms=1500, data=np.zeros(8000, dtype=np.float32),
    )
    assert "data" not in repr(chunk)
    with pytest.raises(ValueError):
        AudioChunk(
            meeting_id=uuid4(), source=Source.SYSTEM, epoch=1, privacy_generation=0,
            chunk_index=0, start_ms=1000, end_ms=1500, data=np.zeros(16000, dtype=np.float32),
        )
    with pytest.raises(ValueError):
        AudioChunk(
            meeting_id=uuid4(), source=Source.SYSTEM, epoch=1, privacy_generation=0,
            chunk_index=0, start_ms=1500, end_ms=1500, data=np.zeros(0, dtype=np.float32),
        )
