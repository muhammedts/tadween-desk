"""Device switching: a switch reports what it actually did, and the notice says so."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from capture_rig import MIC, MIC2, SECOND, SYSTEM, Rig

from meeting_minutes.capture.controller import SwitchOutcome
from meeting_minutes.domain.contracts import Source
from meeting_minutes.domain.states import MeetingState, SourceState
from meeting_minutes.ui import presenter


def device_of(handle):
    return handle._spec.device_id  # the endpoint the fake stream was opened on


def switch(rig, source, device_id):
    return rig.controller.switch_source(source, device_id, expected_epoch=rig.controller.source_epoch(source))


def test_successful_switch_is_applied_to_the_requested_device():
    rig = Rig()
    rig.start()
    result = switch(rig, Source.MIC, MIC2)
    assert (result.outcome, result.previous, result.effective) == (SwitchOutcome.APPLIED, MIC, MIC2)
    assert result.state is SourceState.RUNNING and result.error is None
    assert device_of(rig.handle(Source.MIC)) == MIC2


def test_failed_switch_rolls_back_and_says_so():
    rig = Rig()
    rig.start()
    rig.run(SECOND)
    system_handle = rig.handle(Source.SYSTEM)
    rig.backend.fail_open.add(MIC2)
    result = switch(rig, Source.MIC, MIC2)
    assert result.outcome is SwitchOutcome.ROLLED_BACK and result.state is SourceState.RUNNING
    assert (result.requested, result.effective) == (MIC2, MIC)
    assert result.error == "device_unavailable" and result.rollback_error is None
    assert rig.controller.selected_device(Source.MIC) == MIC
    assert device_of(rig.handle(Source.MIC)) == MIC
    assert rig.handle(Source.SYSTEM) is system_handle and rig.controller.state is MeetingState.CAPTURING


def test_failure_of_both_devices_keeps_the_previous_selection_and_is_not_running():
    rig = Rig()
    rig.start()
    rig.backend.fail_open.update({MIC, MIC2})
    result = switch(rig, Source.MIC, MIC2)
    assert result.outcome is SwitchOutcome.FAILED and result.state is SourceState.FAILED
    assert result.effective == MIC  # the failed request never becomes the selection
    assert (result.error, result.rollback_error) == ("device_unavailable", "device_unavailable")
    assert rig.controller.state is MeetingState.DEGRADED
    assert rig.controller.source_state(Source.SYSTEM) is SourceState.RUNNING
    rig.backend.fail_open.clear()
    retry = rig.controller.reconnect(Source.MIC)
    assert (retry.outcome, retry.effective) == (SwitchOutcome.APPLIED, MIC)
    assert rig.controller.state is MeetingState.CAPTURING


def test_new_device_failing_for_a_lost_source_does_not_replace_its_selection():
    rig = Rig()
    rig.start()
    rig.run(SECOND)
    rig.handle(Source.MIC).lose()
    rig.controller.check_health()
    opened = len(rig.backend.opened)
    rig.backend.fail_open.add(MIC2)
    result = switch(rig, Source.MIC, MIC2)
    assert result.outcome is SwitchOutcome.FAILED and result.rollback_error is None
    assert result.effective == MIC and rig.controller.source_state(Source.MIC) is SourceState.FAILED
    assert len(rig.backend.opened) == opened  # the lost device was not silently reopened


def test_adding_a_source_that_fails_leaves_it_unselected_and_capture_not_degraded():
    rig = Rig()
    rig.start(system=None)
    rig.backend.fail_open.add(SYSTEM)
    result = switch(rig, Source.SYSTEM, SYSTEM)
    assert result.outcome is SwitchOutcome.FAILED and result.previous is None and result.effective is None
    assert rig.controller.source_state(Source.SYSTEM) is SourceState.CLOSED
    assert rig.controller.selected_device(Source.SYSTEM) is None
    assert rig.controller.state is MeetingState.CAPTURING


def test_selection_while_paused_is_recorded_not_opened_until_resume():
    rig = Rig()
    rig.start()
    rig.controller.pause()
    opened = len(rig.backend.opened)
    result = switch(rig, Source.MIC, MIC2)
    assert (result.outcome, result.opens_on, result.effective) == (SwitchOutcome.RECORDED, "resume", MIC2)
    assert len(rig.backend.opened) == opened  # not opened, so not claimed as tested
    rig.controller.resume()
    assert device_of(rig.handle(Source.MIC)) == MIC2


def test_selection_while_mic_excluded_is_recorded_and_the_mic_stays_closed():
    rig = Rig()
    rig.start()
    rig.exclude()
    opened = len(rig.backend.opened)
    result = switch(rig, Source.MIC, MIC2)
    assert (result.outcome, result.opens_on) == (SwitchOutcome.RECORDED, "mic_included")
    assert len(rig.backend.opened) == opened and not rig.controller.mic_included
    assert rig.handle(Source.MIC) is None


NEW, OLD = "wasapi:capture:new-endpoint", "wasapi:capture:old-endpoint"


def notice(**fields):
    base = dict(source=Source.MIC, requested=NEW, previous=OLD, effective=OLD,
                error="device_unavailable", rollback_error=None, opens_on=None)
    return presenter.switch_notice(SimpleNamespace(**{**base, **fields}))


@pytest.mark.parametrize(("fields", "expected"), [
    ({"outcome": SwitchOutcome.APPLIED, "effective": NEW, "error": None}, ""),
    ({"outcome": SwitchOutcome.RECORDED, "effective": NEW, "error": None, "opens_on": "resume"},
     "has not been opened yet and will be tried on Resume"),
    ({"outcome": SwitchOutcome.RECORDED, "effective": NEW, "error": None, "opens_on": "mic_included"},
     "will be tried when the mic is unmuted"),
    ({"outcome": SwitchOutcome.RECORDED, "effective": NEW, "error": None, "opens_on": "start"},
     "will be tried on Start"),
    ({"outcome": SwitchOutcome.ROLLED_BACK}, "(device_unavailable); still capturing from the previous device"),
    ({"outcome": SwitchOutcome.FAILED, "rollback_error": "open_failed"},
     "previous device could not be reopened (open_failed); this source is not capturing"),
    ({"outcome": SwitchOutcome.FAILED}, "the previous device stays selected but is not capturing"),
    ({"outcome": SwitchOutcome.FAILED, "requested": OLD}, "could not be reopened (device_unavailable)"),
    ({"outcome": SwitchOutcome.FAILED, "previous": None, "effective": None}, "this source was not added"),
])
def test_switch_notice_distinguishes_every_outcome_with_fixed_text(fields, expected):
    text = notice(**fields)
    assert (expected in text) if expected else text == ""
    assert NEW not in text and OLD not in text  # fixed wording and codes, no device ids


def test_selection_before_start_is_recorded_for_start():
    rig = Rig()
    result = rig.controller.switch_source(Source.MIC, MIC2, expected_epoch=0)
    assert (result.outcome, result.opens_on, result.effective) == (SwitchOutcome.RECORDED, "start", MIC2)
    assert rig.backend.opened == []


def test_reopen_notice_names_only_selected_sources_that_failed():
    status = {"sources": {
        "mic": {"state": "failed", "device_id": OLD, "failure": "device_unavailable"},
        "system": {"state": "running", "device_id": NEW, "failure": None},
    }}
    text = presenter.reopen_notice(status, "Resume")
    assert text == "Microphone could not be opened on Resume (device_unavailable); choose a device or try again."
    status["sources"]["mic"]["state"] = "running"
    assert presenter.reopen_notice(status, "Resume") == ""
