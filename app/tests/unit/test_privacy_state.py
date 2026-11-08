from __future__ import annotations

import pytest

from meeting_minutes.domain.contracts import Source
from meeting_minutes.domain.privacy import (
    CommandOrigin,
    LocalConfirmationRequired,
    MicInclusion,
    MicPrivacyState,
    StaleGenerationError,
    set_mic_included,
)


def test_initial_state_must_be_chosen_explicitly():
    with pytest.raises(TypeError):
        MicPrivacyState.initial()  # type: ignore[call-arg]
    assert MicPrivacyState.initial(included=False).inclusion is MicInclusion.EXCLUDED
    assert MicPrivacyState.initial(included=True).generation == 0


def test_mute_then_unmute_advances_generation_each_time():
    s0 = MicPrivacyState.initial(included=True)
    muted = set_mic_included(s0, False, expected_generation=0)
    assert muted.changed and muted.current == MicPrivacyState(MicInclusion.EXCLUDED, 1)
    unmuted = set_mic_included(muted.current, True, expected_generation=1)
    assert unmuted.changed and unmuted.current == MicPrivacyState(MicInclusion.INCLUDED, 2)


def test_mic_audio_from_before_exclusion_is_not_admitted_at_any_later_point():
    s0 = MicPrivacyState.initial(included=True)
    assert s0.admits(Source.MIC, 0)
    s1 = set_mic_included(s0, False, expected_generation=0).current
    s2 = set_mic_included(s1, True, expected_generation=1).current
    # Stamped before mute: rejected while muted and still rejected after unmute.
    assert not s1.admits(Source.MIC, 0)
    assert not s2.admits(Source.MIC, 0)
    # Stamped while muted (generation 1) is also never admitted.
    assert not s1.admits(Source.MIC, 1)
    assert not s2.admits(Source.MIC, 1)
    # Only post-unmute audio proceeds.
    assert s2.admits(Source.MIC, 2)


def test_system_source_is_unaffected_by_mic_exclusion():
    muted = set_mic_included(MicPrivacyState.initial(included=True), False, expected_generation=0).current
    assert muted.admits(Source.SYSTEM, 0)
    assert muted.admits(Source.SYSTEM, 99)


def test_repeated_mute_is_idempotent():
    muted = set_mic_included(MicPrivacyState.initial(included=True), False, expected_generation=0).current
    again = set_mic_included(muted, False, expected_generation=1)
    assert not again.changed and again.current.generation == 1


def test_stale_command_is_rejected():
    s1 = set_mic_included(MicPrivacyState.initial(included=True), False, expected_generation=0).current
    with pytest.raises(StaleGenerationError):
        set_mic_included(s1, True, expected_generation=0)


def test_remote_may_exclude_but_not_re_include():
    s0 = MicPrivacyState.initial(included=True)
    s1 = set_mic_included(s0, False, expected_generation=0, origin=CommandOrigin.REMOTE).current
    assert s1.inclusion is MicInclusion.EXCLUDED
    with pytest.raises(LocalConfirmationRequired):
        set_mic_included(s1, True, expected_generation=1, origin=CommandOrigin.REMOTE)
    assert set_mic_included(s1, True, expected_generation=1).current.mic_included


def test_state_is_immutable():
    s0 = MicPrivacyState.initial(included=True)
    with pytest.raises(AttributeError):
        s0.generation = 5  # type: ignore[misc]
