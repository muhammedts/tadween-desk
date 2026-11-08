"""Meeting and per-source lifecycle states and their allowed transitions."""

from __future__ import annotations

from enum import StrEnum


class MeetingState(StrEnum):
    IDLE = "idle"
    PREFLIGHT = "preflight"
    STARTING = "starting"
    CAPTURING = "capturing"
    PAUSED = "paused"
    DEGRADED = "degraded"
    INTERRUPTED = "interrupted"
    STOPPING = "stopping"
    STOPPED = "stopped"


class SourceState(StrEnum):
    CLOSED = "closed"
    OPENING = "opening"
    RUNNING = "running"
    SWITCHING = "switching"
    LOST = "lost"
    FAILED = "failed"


class InvalidTransition(ValueError):
    pass


_M = MeetingState
MEETING_TRANSITIONS: dict[MeetingState, frozenset[MeetingState]] = {
    _M.IDLE: frozenset({_M.PREFLIGHT}),
    _M.PREFLIGHT: frozenset({_M.STARTING, _M.IDLE}),
    # Back to preflight when a selected source fails and no degraded start was chosen.
    _M.STARTING: frozenset({_M.CAPTURING, _M.DEGRADED, _M.STOPPING, _M.PREFLIGHT}),
    _M.CAPTURING: frozenset({_M.PAUSED, _M.DEGRADED, _M.INTERRUPTED, _M.STOPPING}),
    _M.PAUSED: frozenset({_M.CAPTURING, _M.STOPPING}),
    _M.DEGRADED: frozenset({_M.CAPTURING, _M.PAUSED, _M.INTERRUPTED, _M.STOPPING}),
    # Leaving `interrupted` (sleep, crash) requires an explicit user resume or stop.
    _M.INTERRUPTED: frozenset({_M.CAPTURING, _M.STOPPING}),
    _M.STOPPING: frozenset({_M.STOPPED}),
    _M.STOPPED: frozenset(),
}

_S = SourceState
SOURCE_TRANSITIONS: dict[SourceState, frozenset[SourceState]] = {
    _S.CLOSED: frozenset({_S.OPENING}),
    _S.OPENING: frozenset({_S.RUNNING, _S.FAILED, _S.CLOSED}),
    _S.RUNNING: frozenset({_S.SWITCHING, _S.LOST, _S.CLOSED}),
    _S.SWITCHING: frozenset({_S.RUNNING, _S.LOST, _S.FAILED, _S.CLOSED}),
    _S.LOST: frozenset({_S.OPENING, _S.CLOSED}),
    _S.FAILED: frozenset({_S.OPENING, _S.CLOSED}),
}


def require_meeting_transition(current: MeetingState, target: MeetingState) -> MeetingState:
    if target not in MEETING_TRANSITIONS[current]:
        raise InvalidTransition(f"meeting {current} -> {target} not allowed")
    return target


def require_source_transition(current: SourceState, target: SourceState) -> SourceState:
    if target not in SOURCE_TRANSITIONS[current]:
        raise InvalidTransition(f"source {current} -> {target} not allowed")
    return target
