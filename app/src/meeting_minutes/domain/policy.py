"""Data-egress defaults reported by diagnostics. Each choice is separate and off by default."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    cloud_audio_stt: bool = False
    cloud_minutes_text: bool = False
    hosted_text_sync: bool = False
    telemetry: bool = False
    automatic_updates: bool = False

    def as_dict(self) -> dict[str, bool]:
        return asdict(self)


DEFAULT_EGRESS_POLICY = EgressPolicy()
