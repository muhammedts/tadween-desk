"""Transcript text events from local recognition.

Unlike the metadata events in events.py these carry recognized text, so they are
in-memory results for the text consumer only: never logged, and `text` is excluded
from reprs. A partial is a replaceable hypothesis; the final with the same
`hypothesis_id` replaces it. Partials are never persisted or treated as finals.
Times are meeting milliseconds (from Start); `timing_quality` "chunk" means the
range of the decoded chunk, not word timing.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from meeting_minutes.domain.contracts import FIRST_EPOCH, Source
from meeting_minutes.domain.events import GapReason


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TextKind(StrEnum):
    PARTIAL = "partial"
    FINAL = "final"


class TranscriptText(_Frozen):
    kind: TextKind
    meeting_id: UUID
    source: Source
    epoch: int = Field(ge=FIRST_EPOCH)
    privacy_generation: int = Field(ge=0)
    hypothesis_id: str = Field(min_length=1, max_length=64)
    chunk_index: int = Field(ge=0)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str = Field(repr=False)
    # Language reported by the recognizer (e.g. "en", "ar"), if it reports one.
    language: str | None = Field(default=None, max_length=16)
    timing_quality: Literal["chunk"] = "chunk"
    forced_cut: bool = False
    # Leading words removed because they repeated the previous chunk's overlap.
    overlap_trimmed_words: int = Field(default=0, ge=0)
    # A forced-cut overlap whose repeated words could not be aligned: both kept.
    boundary_ambiguous: bool = False
    # Recognizer quality marks (e.g. "lang_uncertain"); never a coverage statement.
    quality_flags: tuple[str, ...] = Field(default=(), max_length=8)


class PartialsCleared(_Frozen):
    """Shown partials of `source` are void: all of them (`through_chunk_index` None,
    e.g. mic exclusion discarded their audio) or those up to a chunk index whose
    final produced no text."""

    source: Source
    through_chunk_index: int | None = Field(default=None, ge=0)


class SpeechGap(_Frozen):
    """Admitted audio that produced no text: released undecoded or failed to decode."""

    source: Source
    epoch: int = Field(ge=FIRST_EPOCH)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    reason: GapReason


TextEvent = TranscriptText | PartialsCleared | SpeechGap
