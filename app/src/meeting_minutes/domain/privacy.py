"""Explicit microphone inclusion state (mic mute/unmute for transcription).

Exclusion is a pre-processing gate on audio identified by `privacy_generation`,
never a filter applied to transcript text. Every mic frame, chunk and result
carries the generation that was current when it entered the pipeline; `admits`
is the commit-time check that rejects anything from an older generation.

This module holds the state and its transition rules; `capture/gate.py`
(PrivacyBoundary) enforces them at the capture, queue hand-off, dispatch and
commit points.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from meeting_minutes.domain.contracts import Source


class MicInclusion(StrEnum):
    INCLUDED = "included"
    EXCLUDED = "excluded"


class CommandOrigin(StrEnum):
    LOCAL = "local"
    REMOTE = "remote"


class StaleGenerationError(RuntimeError):
    """The command was issued against an older privacy generation."""


class LocalConfirmationRequired(PermissionError):
    """Re-including the mic from a remote surface needs local confirmation."""


@dataclass(frozen=True, slots=True)
class MicPrivacyState:
    inclusion: MicInclusion
    generation: int

    @classmethod
    def initial(cls, *, included: bool) -> MicPrivacyState:
        # No default: every meeting starts from an explicit, visible choice.
        return cls(MicInclusion.INCLUDED if included else MicInclusion.EXCLUDED, 0)

    @property
    def mic_included(self) -> bool:
        return self.inclusion is MicInclusion.INCLUDED

    def admits(self, source: Source, generation: int) -> bool:
        """Whether audio or a result stamped with `generation` may proceed or commit."""
        if source is Source.SYSTEM:
            return True
        return self.mic_included and generation == self.generation


@dataclass(frozen=True, slots=True)
class PrivacyTransition:
    previous: MicPrivacyState
    current: MicPrivacyState

    @property
    def changed(self) -> bool:
        return self.previous != self.current


def set_mic_included(
    state: MicPrivacyState,
    included: bool,
    *,
    expected_generation: int,
    origin: CommandOrigin = CommandOrigin.LOCAL,
) -> PrivacyTransition:
    """Compute the next state for an exclude/include command.

    A change increments the generation, which invalidates every mic item stamped
    with the previous one. Repeating the current choice is a no-op, so key repeat
    or a double click cannot advance the generation twice.
    """
    if expected_generation != state.generation:
        raise StaleGenerationError(
            f"expected generation {expected_generation}, current is {state.generation}"
        )
    target = MicInclusion.INCLUDED if included else MicInclusion.EXCLUDED
    if target is state.inclusion:
        return PrivacyTransition(state, state)
    if target is MicInclusion.INCLUDED and origin is not CommandOrigin.LOCAL:
        raise LocalConfirmationRequired("re-including the mic requires local confirmation")
    return PrivacyTransition(state, MicPrivacyState(target, state.generation + 1))
