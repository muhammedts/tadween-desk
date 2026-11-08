"""Source epochs are one-based across every contract."""

from __future__ import annotations

from uuid import UUID, uuid4

import numpy as np
import pytest
from pydantic import ValidationError

from meeting_minutes.capture.fake import FakeAudioBackend
from meeting_minutes.domain.contracts import FIRST_EPOCH, AudioChunk, AudioFrame, DeviceSelection, Source
from meeting_minutes.domain.events import CaptureGapPayload, CaptureStatePayload, GapReason
from meeting_minutes.domain.states import SourceState
from meeting_minutes.storage.store import StorageError, TranscriptStore

MEETING = UUID("daef6350-149b-4b1f-a933-5ebd3a1c95d2")
SELECTIONS = {
    Source.MIC: DeviceSelection(source=Source.MIC, device_id="fake:mic-48k-mono"),
    Source.SYSTEM: DeviceSelection(source=Source.SYSTEM, device_id="fake:system-44k1-stereo"),
}


def first_frames() -> dict[Source, AudioFrame]:
    backend = FakeAudioBackend(meeting_id=MEETING)
    frames: dict[Source, AudioFrame] = {}
    for source, selection in SELECTIONS.items():
        backend.open(selection, lambda f: frames.setdefault(f.source, f)).pump()  # default two-argument open
    return frames


def test_default_fake_epoch_is_one_and_counters_start_at_zero():
    for frame in first_frames().values():
        assert frame.epoch == FIRST_EPOCH == 1
        assert (frame.sequence, frame.first_sample_index, frame.privacy_generation) == (0, 0, 0)


def test_chunk_rejects_epoch_zero():
    with pytest.raises(ValueError):
        AudioChunk(meeting_id=uuid4(), source=Source.MIC, epoch=0, privacy_generation=0,
                   chunk_index=0, start_ms=0, end_ms=10, data=np.zeros(160, dtype=np.float32))


def test_events_reject_epoch_zero_and_accept_one():
    with pytest.raises(ValidationError):
        CaptureStatePayload(source=Source.MIC, state=SourceState.RUNNING, epoch=0)
    with pytest.raises(ValidationError):
        CaptureGapPayload(source=Source.MIC, epoch=0, start_ms=0, end_ms=1, reason=GapReason.DEVICE_SWITCH)
    assert CaptureStatePayload(source=Source.MIC, state=SourceState.RUNNING, epoch=1).epoch == 1
    assert CaptureGapPayload(source=Source.SYSTEM, epoch=1, start_ms=0, end_ms=1, reason=GapReason.DEVICE_SWITCH).epoch == 1


def test_the_saved_schema_stores_the_first_fake_epoch_and_refuses_epoch_zero(tmp_path):
    store = TranscriptStore(tmp_path / "meetings.db")
    store.create_meeting(str(MEETING), title="Epochs", timezone_name="UTC", started_at="2026-01-01T10:00:00.000+00:00")
    for source, frame in first_frames().items():
        store.save_epoch(str(MEETING), source=source.value, epoch=frame.epoch, device_id=SELECTIONS[source].device_id,
                         device_label="fake", native_rate=48_000, native_channels=1, start_ms=0, end_ms=None)
    with pytest.raises(StorageError):  # source_epochs.epoch has CHECK(epoch >= 1)
        store.save_epoch(str(MEETING), source="mic", epoch=0, device_id="d", device_label="d", native_rate=48_000,
                         native_channels=1, start_ms=0, end_ms=None)
    store.close()
