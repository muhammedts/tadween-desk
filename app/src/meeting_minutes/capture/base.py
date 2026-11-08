"""Capture backend protocol implemented by the WASAPI and fake backends."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from meeting_minutes.domain.contracts import AudioFrame, DeviceDescriptor, DeviceSelection

# Called on the capture path: must not do inference, network or database I/O.
FrameCallback = Callable[[AudioFrame], None]
# Monotonic nanoseconds; injectable so tests control time.
Clock = Callable[[], int]


class DeviceUnavailable(LookupError):
    """The selected endpoint is not present. Never answered by choosing another device."""


class AmbiguousDevice(LookupError):
    """The saved selection matches several endpoints; the user must reselect."""


class FormatUnsupported(ValueError):
    """The endpoint refused its own reported mix format; no other format is tried silently."""


class CaptureHandle(Protocol):
    def stop(self) -> None: ...

    def close(self) -> None: ...

    def is_active(self) -> bool: ...


class CaptureBackend(Protocol):
    def enumerate(self) -> list[DeviceDescriptor]: ...

    def open(
        self,
        selection: DeviceSelection,
        callback: FrameCallback,
        *,
        meeting_id: UUID,
        epoch: int,
        privacy_generation: Callable[[], int],
        clock: Clock,
    ) -> CaptureHandle: ...
