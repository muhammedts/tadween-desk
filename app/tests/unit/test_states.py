from __future__ import annotations

import pytest

from meeting_minutes.domain.states import (
    MEETING_TRANSITIONS,
    SOURCE_TRANSITIONS,
    InvalidTransition,
    MeetingState,
    SourceState,
    require_meeting_transition,
    require_source_transition,
)


def test_every_state_has_a_transition_entry():
    assert set(MEETING_TRANSITIONS) == set(MeetingState)
    assert set(SOURCE_TRANSITIONS) == set(SourceState)


def test_normal_meeting_path():
    path = [
        MeetingState.IDLE, MeetingState.PREFLIGHT, MeetingState.STARTING,
        MeetingState.CAPTURING, MeetingState.PAUSED, MeetingState.CAPTURING,
        MeetingState.STOPPING, MeetingState.STOPPED,
    ]
    for current, target in zip(path, path[1:]):
        assert require_meeting_transition(current, target) is target


def test_every_non_terminal_meeting_state_can_reach_stopped():
    for state in MeetingState:
        seen, frontier = {state}, [state]
        while frontier:
            for nxt in MEETING_TRANSITIONS[frontier.pop()]:
                if nxt not in seen:
                    seen.add(nxt)
                    frontier.append(nxt)
        assert MeetingState.STOPPED in seen


@pytest.mark.parametrize(
    "current,target",
    [
        (MeetingState.STOPPED, MeetingState.CAPTURING),
        (MeetingState.IDLE, MeetingState.CAPTURING),
        (MeetingState.INTERRUPTED, MeetingState.PAUSED),
    ],
)
def test_invalid_meeting_transitions(current, target):
    with pytest.raises(InvalidTransition):
        require_meeting_transition(current, target)


def test_lost_source_never_returns_to_running_without_reopening():
    with pytest.raises(InvalidTransition):
        require_source_transition(SourceState.LOST, SourceState.RUNNING)
    assert require_source_transition(SourceState.LOST, SourceState.OPENING) is SourceState.OPENING
