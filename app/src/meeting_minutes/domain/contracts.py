"""Device, frame and chunk contracts shared by capture, speech and storage.

Device metadata is a JSON-serialisable Pydantic model. PCM carriers (AudioFrame,
AudioChunk) are deliberately plain dataclasses: they are not JSON-serialisable and
their reprs never include sample data.

Ownership: a frame or chunk copies the samples it is given into a new, tightly
sized, C-contiguous, read-only array that nothing else references. Producers may
therefore reuse their capture buffers immediately, and `nbytes` is exactly the
storage the carrier retains (a small view cannot pin a larger allocation).

Epochs are one-based (the `source_epochs.epoch >= 1` database constraint); privacy
generations, sequences and sample indices are zero-based.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, field_validator

TARGET_SAMPLE_RATE = 16_000
NS_PER_SECOND = 1_000_000_000
FIRST_EPOCH = 1


class Source(StrEnum):
    MIC = "mic"
    SYSTEM = "system"


class Direction(StrEnum):
    CAPTURE = "capture"
    RENDER = "render"


class IdentityStability(StrEnum):
    """Whether a device_id survives re-enumeration."""

    STABLE_ENDPOINT_ID = "stable_endpoint_id"
    # Requires user reselection after ambiguous enumeration.
    UNSTABLE_FINGERPRINT = "unstable_fingerprint"


class TimingQuality(StrEnum):
    DEVICE = "device"
    CALLBACK = "callback"
    SYNTHETIC = "synthetic"


class AudioFormat(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sample_rate: int = Field(ge=8_000, le=384_000)
    channels: int = Field(ge=1, le=32)
    sample_dtype: str = Field(default="float32", pattern=r"^(float32|int16|int32)$")


class DeviceDescriptor(BaseModel):
    """Enumerated endpoint metadata. Never contains hardware serials."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: str = Field(min_length=1, max_length=512)
    display_name: str = Field(min_length=1, max_length=256)
    backend: str = Field(min_length=1, max_length=64)
    direction: Direction
    identity_stability: IdentityStability
    supports_loopback: bool
    endpoint_binding: str | None = None
    default_roles: tuple[str, ...] = ()
    probed_formats: tuple[AudioFormat, ...] = ()
    available: bool = True
    # Fixed code explaining why an endpoint cannot be captured (e.g. no_loopback,
    # ambiguous_loopback); never free text from the device.
    unavailable_reason: str | None = Field(default=None, pattern=r"^[a-z_]{1,64}$")

    @field_validator("device_id")
    @classmethod
    def _not_an_enumeration_index(cls, value: str) -> str:
        # A PortAudio numeric index is not an identity and must never be persisted.
        if value.strip().isdigit():
            raise ValueError("device_id must be opaque, not a numeric enumeration index")
        return value


class DeviceSelection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Source
    device_id: str = Field(min_length=1, max_length=512)


def _check_nonnegative(**values: int) -> None:
    for name, value in values.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{name} must be a non-negative int")


def _check_epoch(epoch: int) -> None:
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < FIRST_EPOCH:
        raise ValueError(f"epoch must be an int >= {FIRST_EPOCH}")


def _owned_readonly_copy(data: np.ndarray) -> np.ndarray:
    """Copy into a new contiguous array that owns its memory and cannot be written."""
    owned = np.array(data, dtype=np.float32, order="C", copy=True)
    owned.flags.writeable = False
    return owned


@dataclass(frozen=True, slots=True, eq=False)
class AudioFrame:
    """One native capture buffer for one source, held in memory only.

    `first_sample_index` counts native sample frames since the start of `epoch`;
    `monotonic_ns` is the capture time of that first sample.
    """

    meeting_id: UUID
    source: Source
    epoch: int
    privacy_generation: int
    sequence: int
    first_sample_index: int
    sample_rate: int
    channels: int
    monotonic_ns: int
    timing_quality: TimingQuality
    data: np.ndarray = field(repr=False)
    flags: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        _check_epoch(self.epoch)
        _check_nonnegative(
            privacy_generation=self.privacy_generation,
            sequence=self.sequence,
            first_sample_index=self.first_sample_index,
            monotonic_ns=self.monotonic_ns,
        )
        if not 8_000 <= self.sample_rate <= 384_000:
            raise ValueError("sample_rate out of range")
        if not 1 <= self.channels <= 32:
            raise ValueError("channels out of range")
        if self.data.dtype != np.float32 or self.data.ndim != 2 or self.data.shape[1] != self.channels:
            raise ValueError("data must be float32 with shape (frame_count, channels)")
        if self.data.shape[0] == 0:
            raise ValueError("frame must contain at least one sample frame")
        object.__setattr__(self, "data", _owned_readonly_copy(self.data))

    @property
    def frame_count(self) -> int:
        return int(self.data.shape[0])

    @property
    def end_sample_index(self) -> int:
        return self.first_sample_index + self.frame_count

    @property
    def nbytes(self) -> int:
        return int(self.data.nbytes)

    @property
    def duration_ns(self) -> int:
        return self.frame_count * NS_PER_SECOND // self.sample_rate


@dataclass(frozen=True, slots=True, eq=False)
class AudioChunk:
    """Bounded 16 kHz mono window passed to a recognizer, with its time lineage."""

    meeting_id: UUID
    source: Source
    epoch: int
    privacy_generation: int
    chunk_index: int
    start_ms: int
    end_ms: int
    data: np.ndarray = field(repr=False)
    overlap_ms: int = 0
    forced_cut: bool = False

    def __post_init__(self) -> None:
        _check_epoch(self.epoch)
        _check_nonnegative(
            privacy_generation=self.privacy_generation,
            chunk_index=self.chunk_index,
            start_ms=self.start_ms,
            end_ms=self.end_ms,
            overlap_ms=self.overlap_ms,
        )
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be after start_ms")
        if self.data.dtype != np.float32 or self.data.ndim != 1:
            raise ValueError("chunk data must be 1-D float32 at 16 kHz")
        samples_per_ms = TARGET_SAMPLE_RATE // 1000
        if abs(self.data.shape[0] - (self.end_ms - self.start_ms) * samples_per_ms) > samples_per_ms:
            raise ValueError("chunk sample count does not match its time range")
        if self.overlap_ms > self.end_ms - self.start_ms:
            raise ValueError("overlap cannot exceed chunk duration")
        object.__setattr__(self, "data", _owned_readonly_copy(self.data))
