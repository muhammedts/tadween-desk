from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from meeting_minutes.domain.contracts import Source
from meeting_minutes.domain.events import (
    CaptureGapEvent,
    CaptureGapPayload,
    GapReason,
    MicPrivacyCode,
    MicPrivacyPayload,
    MicPrivacyStateEvent,
    parse_event,
)


def privacy_event(**payload) -> MicPrivacyStateEvent:
    return MicPrivacyStateEvent(
        event_id=uuid4(), meeting_id=uuid4(), stream_seq=3,
        payload=MicPrivacyPayload(code=MicPrivacyCode.MIC_EXCLUDED, privacy_generation=1, at_ms=45_100, **payload),
    )


def test_privacy_event_round_trips_through_discriminated_parser():
    event = privacy_event(discarded_pending_ms=1200)
    parsed = parse_event(event.model_dump_json())
    assert isinstance(parsed, MicPrivacyStateEvent)
    assert parsed == event
    assert json.loads(event.model_dump_json())["protocol_version"] == 1


def test_privacy_event_cannot_carry_text_or_audio():
    raw = json.loads(privacy_event().model_dump_json())
    raw["payload"]["text"] = "private side conversation"
    with pytest.raises(ValidationError):
        parse_event(json.dumps(raw))
    raw["payload"].pop("text")
    raw["payload"]["audio"] = "AAAA"
    with pytest.raises(ValidationError):
        parse_event(json.dumps(raw))


def test_unknown_protocol_version_and_type_rejected():
    raw = json.loads(privacy_event().model_dump_json())
    with pytest.raises(ValidationError):
        parse_event(json.dumps({**raw, "protocol_version": 2}))
    with pytest.raises(ValidationError):
        parse_event(json.dumps({**raw, "type": "segment.final"}))


def test_gap_must_be_ordered():
    with pytest.raises(ValidationError):
        CaptureGapPayload(source=Source.MIC, epoch=1, start_ms=10, end_ms=5, reason=GapReason.QUEUE_OVERFLOW)
    gap = CaptureGapEvent(
        event_id=uuid4(), meeting_id=uuid4(), stream_seq=0,
        payload=CaptureGapPayload(source=Source.SYSTEM, epoch=2, start_ms=5, end_ms=10, reason=GapReason.DEVICE_SWITCH),
    )
    assert isinstance(parse_event(gap.model_dump_json()), CaptureGapEvent)
