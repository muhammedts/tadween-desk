"""Durable metadata events (protocol_version 1).

Capture state, gap, mic privacy and device-change events emitted by the capture
controller. Payloads forbid unknown fields, so these events cannot carry
transcript text or audio.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from meeting_minutes.domain.contracts import FIRST_EPOCH, Source
from meeting_minutes.domain.states import SourceState


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GapReason(StrEnum):
    QUEUE_OVERFLOW = "queue_overflow"
    DEVICE_SWITCH = "device_switch"
    DEVICE_LOST = "device_lost"
    MIC_EXCLUDED_PENDING_DISCARDED = "mic_excluded_pending_discarded"
    STOPPED_UNDRAINED = "stopped_undrained"
    # The endpoint delivered no samples for longer than callback jitter explains. For
    # playback loopback this is normal while nothing plays (WASAPI sends no packets);
    # for a microphone it is a dropout. Either way the interval holds no audio.
    NO_DATA_DELIVERED = "no_data_delivered"
    # The global owned-PCM budget across sources forced the oldest audio out.
    PCM_BUDGET = "pcm_budget"
    # The native driver reported discarded input (PortAudio paInputOverflow). Its length
    # is known only when the sample clock also saw the time jump; otherwise the gap is a
    # zero-width marker with `duration_known = false`.
    INPUT_OVERFLOW = "input_overflow"
    # Downstream processing raised while taking this audio; the frame was consumed.
    PROCESSING_FAILED = "processing_failed"
    # Speech recognition: admitted audio that fell behind the bounded
    # recognition backlog and was released undecoded, or whose decode raised.
    STT_BACKLOG = "stt_backlog"
    STT_FAILED = "stt_failed"


class MicPrivacyCode(StrEnum):
    MIC_EXCLUDED = "mic_excluded"
    MIC_INCLUDED = "mic_included"


class StateReason(StrEnum):
    STARTED = "started"
    PAUSED = "paused"
    RESUMED = "resumed"
    STOPPED = "stopped"
    MIC_EXCLUDED = "mic_excluded"
    MIC_INCLUDED = "mic_included"
    DEVICE_SWITCH = "device_switch"
    DEVICE_LOST = "device_lost"
    OPEN_FAILED = "open_failed"
    RECONNECTED = "reconnected"


class CaptureStatePayload(_Frozen):
    source: Source
    state: SourceState
    epoch: int = Field(ge=FIRST_EPOCH)
    reason: StateReason | None = None
    at_ms: int | None = Field(default=None, ge=0)


class CaptureGapPayload(_Frozen):
    source: Source
    epoch: int = Field(ge=FIRST_EPOCH)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    reason: GapReason
    # False when audio is known to be missing at start_ms but its length is not known.
    duration_known: bool = True

    @model_validator(mode="after")
    def _ordered(self) -> CaptureGapPayload:
        if self.end_ms < self.start_ms:
            raise ValueError("gap end_ms precedes start_ms")
        if not self.duration_known and self.end_ms != self.start_ms:
            raise ValueError("a gap of unknown duration is a point marker (end_ms == start_ms)")
        return self


class MicPrivacyPayload(_Frozen):
    code: MicPrivacyCode
    privacy_generation: int = Field(ge=0)
    at_ms: int = Field(ge=0)
    # Pending, uncommitted pre-toggle mic audio that was discarded; shown to the user.
    discarded_pending_ms: int = Field(default=0, ge=0)


class SourceChangedPayload(_Frozen):
    source: Source
    epoch: int = Field(ge=FIRST_EPOCH)
    device_id: str = Field(min_length=1, max_length=512)
    sample_rate: int = Field(ge=8_000, le=384_000)
    channels: int = Field(ge=1, le=32)
    at_ms: int = Field(ge=0)


class _Envelope(_Frozen):
    protocol_version: Literal[1] = 1
    event_id: UUID
    meeting_id: UUID
    stream_seq: int = Field(ge=0)


class CaptureStateEvent(_Envelope):
    type: Literal["capture.state"] = "capture.state"
    payload: CaptureStatePayload


class CaptureGapEvent(_Envelope):
    type: Literal["capture.gap"] = "capture.gap"
    payload: CaptureGapPayload


class MicPrivacyStateEvent(_Envelope):
    type: Literal["mic.privacy_state"] = "mic.privacy_state"
    payload: MicPrivacyPayload


class SourceChangedEvent(_Envelope):
    type: Literal["source.changed"] = "source.changed"
    payload: SourceChangedPayload


Event = Annotated[
    Union[CaptureStateEvent, CaptureGapEvent, MicPrivacyStateEvent, SourceChangedEvent],
    Field(discriminator="type"),
]
EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)


def parse_event(raw: str | bytes) -> Event:
    return EVENT_ADAPTER.validate_json(raw)
