"""WASAPI backend logic against a scripted PyAudioWPatch stand-in (no hardware)."""

from __future__ import annotations

from uuid import UUID

import numpy as np
import pytest
from fake_pyaudio import device, make_module

from meeting_minutes.capture.base import AmbiguousDevice, DeviceUnavailable, FormatUnsupported
from meeting_minutes.capture.wasapi import WasapiBackend, fingerprint
from meeting_minutes.domain.contracts import (
    DeviceSelection,
    Direction,
    IdentityStability,
    Source,
    TimingQuality,
)

MEETING = UUID(int=7)


def by_name(devices):
    return {(d.direction, d.display_name): d for d in devices}


def open_source(backend, source, device_id, frames, *, now=None, generation=lambda: 0, epoch=1):
    clock = now or (lambda: 1_000_000_000)
    return backend.open(
        DeviceSelection(source=source, device_id=device_id), frames.append,
        meeting_id=MEETING, epoch=epoch, privacy_generation=generation, clock=clock,
    )


def test_enumerates_wasapi_mics_and_render_endpoints_with_loopback_formats():
    devices = WasapiBackend(pyaudio_module=make_module()).enumerate()
    found = by_name(devices)
    assert {(d.direction, d.display_name) for d in devices} == {
        (Direction.CAPTURE, "Headset Microphone (Poly BT700)"),
        (Direction.CAPTURE, "Microphone Array (AMD Audio Device)"),
        (Direction.RENDER, "Speakers (Realtek(R) Audio)"),
        (Direction.RENDER, "Headset Earphone (Poly BT700)"),
    }
    headset_mic = found[(Direction.CAPTURE, "Headset Microphone (Poly BT700)")]
    assert [(f.sample_rate, f.channels) for f in headset_mic.probed_formats] == [(16_000, 1)]
    speakers = found[(Direction.RENDER, "Speakers (Realtek(R) Audio)")]
    assert speakers.supports_loopback and speakers.available
    assert [(f.sample_rate, f.channels) for f in speakers.probed_formats] == [(48_000, 2)]
    assert speakers.default_roles == ("default",)
    assert found[(Direction.CAPTURE, "Microphone Array (AMD Audio Device)")].default_roles == ("default",)


def test_ids_are_name_fingerprints_not_indices_and_marked_unstable():
    devices = WasapiBackend(pyaudio_module=make_module()).enumerate()
    for d in devices:
        assert d.identity_stability is IdentityStability.UNSTABLE_FINGERPRINT
        assert d.device_id == fingerprint(d.direction, d.display_name)
        assert not any(part.isdigit() and len(part) <= 3 for part in d.device_id.split(":"))


def test_id_survives_reenumeration_with_changed_indices():
    module = make_module()
    backend = WasapiBackend(pyaudio_module=module)
    mic_id = by_name(backend.enumerate())[(Direction.CAPTURE, "Headset Microphone (Poly BT700)")].device_id
    module.devices = [dict(d, index=d["index"] + 10) for d in module.devices]
    module.default_input = 27
    assert mic_id in {d.device_id for d in backend.enumerate()}
    frames = []
    handle = open_source(backend, Source.MIC, mic_id, frames)
    assert module.streams[-1].kwargs["input_device_index"] == 26  # the new index
    handle.close()


def test_loopback_analogue_matched_exactly_not_by_substring():
    devices = [
        device(1, "Speakers", outputs=2),
        device(2, "Speakers (USB)", outputs=2),
        device(3, "Speakers (USB) [Loopback]", inputs=2, loopback=True),
    ]
    found = by_name(WasapiBackend(pyaudio_module=make_module(devices, default_output=1)).enumerate())
    # PyAudioWPatch's substring helper would hand "Speakers" the USB loopback.
    assert not found[(Direction.RENDER, "Speakers")].supports_loopback
    assert not found[(Direction.RENDER, "Speakers")].available
    assert found[(Direction.RENDER, "Speakers (USB)")].supports_loopback


def test_duplicate_names_require_reselection_after_refresh():
    devices = [
        device(1, "Headset Microphone (USB)", inputs=1),
        device(2, "Headset Microphone (USB)", inputs=1),
        device(3, "Speakers", outputs=2),
        device(4, "Speakers [Loopback]", inputs=2, loopback=True),
    ]
    module = make_module(devices, default_input=1, default_output=3)
    backend = WasapiBackend(pyaudio_module=module)
    mics = [d for d in backend.enumerate() if d.direction is Direction.CAPTURE]
    assert len({d.device_id for d in mics}) == 2
    assert all("#" in d.device_id for d in mics)
    frames = []
    open_source(backend, Source.MIC, mics[1].device_id, frames).close()  # same snapshot: usable
    backend.enumerate()  # refresh: ordinals may now point at the other headset
    with pytest.raises(AmbiguousDevice):
        open_source(backend, Source.MIC, mics[1].device_id, frames)
    # The plain fingerprint is ambiguous too; never resolved to one of them silently.
    with pytest.raises(AmbiguousDevice):
        open_source(backend, Source.MIC, fingerprint(Direction.CAPTURE, "Headset Microphone (USB)"), frames)


def test_missing_device_and_wrong_direction_never_fall_back():
    module = make_module()
    backend = WasapiBackend(pyaudio_module=module)
    devices = by_name(backend.enumerate())
    with pytest.raises(DeviceUnavailable):
        open_source(backend, Source.MIC, fingerprint(Direction.CAPTURE, "Unplugged mic"), [])
    speakers = devices[(Direction.RENDER, "Speakers (Realtek(R) Audio)")].device_id
    with pytest.raises(ValueError):
        open_source(backend, Source.MIC, speakers, [])
    assert module.streams == []


def test_system_source_opens_the_loopback_analogue_at_its_mix_format():
    module = make_module()
    backend = WasapiBackend(pyaudio_module=module)
    speakers = by_name(backend.enumerate())[(Direction.RENDER, "Speakers (Realtek(R) Audio)")].device_id
    handle = open_source(backend, Source.SYSTEM, speakers, [])
    kwargs = module.streams[-1].kwargs
    assert kwargs["input_device_index"] == 18
    assert (kwargs["rate"], kwargs["channels"], kwargs["format"]) == (48_000, 2, module.paFloat32)
    assert kwargs["input"] is True and kwargs["frames_per_buffer"] == 480
    assert module.streams[-1].started
    assert module.format_checks[-1] == (48_000, 18, 2, module.paFloat32)
    handle.close()


def test_unsupported_format_is_reported_and_no_other_format_is_tried():
    module = make_module()
    module.refuse_format = {16}
    backend = WasapiBackend(pyaudio_module=module)
    mic = by_name(backend.enumerate())[(Direction.CAPTURE, "Headset Microphone (Poly BT700)")].device_id
    with pytest.raises(FormatUnsupported):
        open_source(backend, Source.MIC, mic, [])
    assert len(module.format_checks) == 1 and module.streams == []


def test_open_failure_maps_to_unavailable_without_device_details():
    module = make_module()
    module.refuse_open = {16}
    backend = WasapiBackend(pyaudio_module=module)
    mic = by_name(backend.enumerate())[(Direction.CAPTURE, "Headset Microphone (Poly BT700)")].device_id
    with pytest.raises(DeviceUnavailable) as info:
        open_source(backend, Source.MIC, mic, [])
    assert "Poly" not in str(info.value) and info.value.__cause__ is None


def test_callback_builds_owned_frames_with_sample_clock_timing():
    module = make_module()
    backend = WasapiBackend(pyaudio_module=module)
    mic = by_name(backend.enumerate())[(Direction.CAPTURE, "Microphone Array (AMD Audio Device)")].device_id
    frames, now = [], [2_000_000_000]
    handle = open_source(backend, Source.MIC, mic, frames, now=lambda: now[0], generation=lambda: 5, epoch=3)
    callback = module.streams[-1].callback
    native = bytearray(np.arange(960, dtype=np.float32).tobytes())  # 480 frames x 2 channels
    assert callback(bytes(native), 480, {}, 0) == (None, module.paContinue)
    now[0] += 10_000_000
    native[:] = np.full(960, -1, dtype=np.float32).tobytes()  # producer reuses its buffer
    callback(bytes(native), 480, {}, module.paInputOverflow)
    first, second = frames
    assert first.data.shape == (480, 2) and first.data[1, 1] == 3.0 and not first.data.flags.writeable
    assert (first.epoch, first.privacy_generation, first.sequence) == (3, 5, 0)
    assert (first.first_sample_index, second.first_sample_index) == (0, 480)
    assert second.monotonic_ns - first.monotonic_ns == 10_000_000
    assert first.timing_quality is TimingQuality.CALLBACK
    assert "input_overflow" in second.flags and handle.overflows == 1


def test_callback_flags_no_data_interval_as_discontinuity():
    module = make_module()
    backend = WasapiBackend(pyaudio_module=module)
    speakers = by_name(backend.enumerate())[(Direction.RENDER, "Speakers (Realtek(R) Audio)")].device_id
    frames, now = [], [1_000_000_000]
    open_source(backend, Source.SYSTEM, speakers, frames, now=lambda: now[0])
    callback = module.streams[-1].callback
    buffer = np.zeros(960, dtype=np.float32).tobytes()
    callback(buffer, 480, {}, 0)
    now[0] += 2_010_000_000  # nothing played for two seconds
    callback(buffer, 480, {}, 0)
    assert "discontinuity" in frames[1].flags
    assert frames[1].first_sample_index == 480 + 2 * 48_000


def test_callback_never_raises_into_portaudio():
    module = make_module()
    backend = WasapiBackend(pyaudio_module=module)
    mic = by_name(backend.enumerate())[(Direction.CAPTURE, "Headset Microphone (Poly BT700)")].device_id

    def broken_consumer(frame):
        raise RuntimeError("downstream failure")

    handle = backend.open(
        DeviceSelection(source=Source.MIC, device_id=mic), broken_consumer,
        meeting_id=MEETING, epoch=1, privacy_generation=lambda: 0, clock=lambda: 1_000_000_000,
    )
    callback = module.streams[-1].callback
    assert callback(np.zeros(160, dtype=np.float32).tobytes(), 160, {}, 0) == (None, module.paContinue)
    assert callback(b"\x00" * 7, 160, {}, 0) == (None, module.paContinue)  # malformed size
    assert (handle.callback_errors, handle.malformed) == (1, 1)


def test_stop_close_and_backend_close_release_everything():
    module = make_module()
    backend = WasapiBackend(pyaudio_module=module)
    devices = by_name(backend.enumerate())
    mic = devices[(Direction.CAPTURE, "Headset Microphone (Poly BT700)")].device_id
    speakers = devices[(Direction.RENDER, "Speakers (Realtek(R) Audio)")].device_id
    handles = [open_source(backend, Source.MIC, mic, []), open_source(backend, Source.SYSTEM, speakers, [])]
    assert all(h.is_active() for h in handles)
    handles[0].close()
    handles[0].close()  # idempotent
    assert module.streams[0].closed and not handles[0].is_active()
    # Once stopping, a late callback asks PortAudio to finish instead of producing frames.
    assert module.streams[0].callback(b"", 0, {}, 0) == (None, module.paComplete)
    backend.close()
    assert all(s.closed for s in module.streams)
    assert module.instances[-1].terminated


def test_refresh_is_deferred_while_streams_are_open():
    module = make_module()
    backend = WasapiBackend(pyaudio_module=module)
    mic = by_name(backend.enumerate())[(Direction.CAPTURE, "Headset Microphone (Poly BT700)")].device_id
    handle = open_source(backend, Source.MIC, mic, [])
    backend.enumerate()
    assert len(module.instances) == 1  # PortAudio was not restarted under an open stream
    handle.close()
    backend.enumerate()
    assert len(module.instances) == 2 and module.instances[0].terminated


def test_wasapi_unavailable_gives_an_empty_list():
    module = make_module()
    module.wasapi_available = False
    assert WasapiBackend(pyaudio_module=module).enumerate() == []
