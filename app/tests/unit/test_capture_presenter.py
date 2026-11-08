from __future__ import annotations

from capture_rig import SECOND, Rig

from meeting_minutes.domain.contracts import Source
from meeting_minutes.ui import presenter


def test_mic_banner_and_button_wording_distinguish_app_mute_from_meeting_mute():
    rig = Rig()
    status = rig.controller.status()
    assert presenter.mic_banner(status) == "Mic included in transcription"
    assert presenter.mic_button_text(status) == "Mute mic"
    rig.exclude()
    status = rig.controller.status()
    assert presenter.mic_banner(status).startswith("Mic excluded from transcription")
    assert "meeting audio continues" in presenter.mic_banner(status)
    assert presenter.mic_button_text(status) == "Unmute mic"
    assert "does not mute Teams, Zoom or Meet" in presenter.ROUTING_NOTE
    assert "Pause all" in presenter.ROUTING_NOTE


def test_controls_follow_meeting_state_and_mute_is_always_available():
    rig = Rig()
    idle = presenter.controls(rig.controller.status(), mic_selected=True, system_selected=True)
    assert idle["start"] and idle["mic"] and not idle["stop"] and not idle["pause"]
    assert not presenter.controls(rig.controller.status(), mic_selected=False, system_selected=False)["start"]
    rig.start()
    live = presenter.controls(rig.controller.status(), mic_selected=True, system_selected=True)
    assert not live["start"] and live["pause"] and live["stop"] and live["mic"] and not live["devices"]
    rig.controller.pause()
    paused = presenter.controls(rig.controller.status(), mic_selected=True, system_selected=True)
    assert paused["resume"] and paused["stop"] and paused["mic"]


def test_excluded_mic_is_shown_as_not_captured_rather_than_silent():
    rig = Rig()
    rig.start()
    rig.run(SECOND)
    assert presenter.source_line("mic", rig.controller.status()).startswith("Microphone: capturing")
    rig.exclude()
    assert presenter.source_line("mic", rig.controller.status()) == "Microphone: not captured (excluded)"
    assert presenter.source_line("system", rig.controller.status()).startswith("System audio: capturing")


def test_lost_and_failed_sources_are_explicit():
    rig = Rig()
    rig.start()
    rig.run(SECOND)
    rig.handle(Source.SYSTEM).lose()
    rig.controller.check_health()
    assert "LOST" in presenter.source_line("system", rig.controller.status())


def test_event_lines_are_metadata_only():
    rig = Rig()
    rig.start()
    rig.run(SECOND)
    rig.exclude()
    rig.include()
    lines = [presenter.event_line(e) for e in rig.controller.events()]
    assert any("mic excluded" in line for line in lines)
    assert any("mic included (fresh start)" in line for line in lines)
    assert all("fake:" not in line for line in lines)  # no device identifiers in the timeline


def test_meter_fraction_bounds():
    assert presenter.meter_fraction(-120.0) == 0.0
    assert presenter.meter_fraction(0.0) == 1.0
    assert presenter.meter_fraction(-30.0) == 0.5


def test_idle_system_audio_is_not_reported_as_loss():
    rig = Rig()
    rig.start()
    rig.handle(Source.SYSTEM).stop()  # nothing playing
    rig.run(2 * SECOND)
    rig.controller.pause()
    line = presenter.source_line("system", rig.controller.status())
    assert "no audio delivered 2.0 s" in line and "lost" not in line


def test_before_start_lines_reflect_the_pickers_not_capture():
    rig = Rig()
    status = rig.controller.status()
    assert presenter.source_line("mic", status, picked=True).endswith("press Start to capture")
    assert presenter.source_line("system", status, picked=False) == "System audio: not selected"
    rig.exclude()
    assert "stay excluded" in presenter.source_line("mic", rig.controller.status(), picked=True)
