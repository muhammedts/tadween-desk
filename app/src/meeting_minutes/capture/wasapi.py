"""Windows WASAPI microphone and playback-loopback capture via PyAudioWPatch.

Only the WASAPI host API is used, so MME and
DirectSound duplicates of the same endpoints never appear. A capture endpoint is a
microphone source; a render endpoint is a system source captured through its
`<name> [Loopback]` analogue, matched by exact name (PyAudioWPatch's own helper
matches by substring, which can pick the wrong one of two similar names).

Identity: PortAudio exposes enumeration indices and names, not Windows
endpoint IDs. Device ids are therefore explicit fingerprints of direction and name
(`IdentityStability.UNSTABLE_FINGERPRINT`), never indices. A fingerprint that matches
exactly one endpoint survives refreshes. Endpoints whose names collide get an
ordinal and the enumeration snapshot in their id; such an id stops resolving after a
refresh (`AmbiguousDevice`) so the user must reselect rather than risk capturing the
other headset. A render endpoint is mapped to a loopback stream only when exactly
one render endpoint and exactly one loopback carry that name; otherwise the pairing
cannot be proven from PortAudio's data, so the endpoint is listed as unavailable
(`ambiguous_loopback`) and refuses to open. Opening never
falls back to a different endpoint or format.

The native callback copies the buffer into an owned `AudioFrame`, assigns its
sample position and capture time from a `SampleClock`, and hands it to the
controller's gate. It does no inference, I/O or blocking work, and never raises into
PortAudio's thread. PyAudioWPatch is imported only when this backend is used.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType
from typing import Any
from uuid import UUID

import numpy as np

from meeting_minutes.capture.base import (
    AmbiguousDevice,
    Clock,
    DeviceUnavailable,
    FormatUnsupported,
    FrameCallback,
)
from meeting_minutes.capture.clock import SampleClock
from meeting_minutes.domain.contracts import (
    AudioFormat,
    AudioFrame,
    DeviceDescriptor,
    DeviceSelection,
    Direction,
    IdentityStability,
    Source,
    TimingQuality,
)

BACKEND = "wasapi"
LOOPBACK_SUFFIX = " [Loopback]"
BUFFER_SECONDS = 0.01  # 10 ms native buffers

_REQUIRED_DIRECTION = {Source.MIC: Direction.CAPTURE, Source.SYSTEM: Direction.RENDER}


def _load_pyaudio() -> ModuleType:
    import pyaudiowpatch

    return pyaudiowpatch


def fingerprint(direction: Direction, name: str) -> str:
    digest = hashlib.sha256(f"{direction.value}\0{name}".encode("utf-8")).hexdigest()[:20]
    return f"{BACKEND}:{direction.value}:{digest}"


NO_LOOPBACK = "no_loopback"
AMBIGUOUS_LOOPBACK = "ambiguous_loopback"
AUDIO_SYSTEM_UNAVAILABLE = "audio_system_unavailable"


@dataclass(frozen=True, slots=True)
class _Endpoint:
    descriptor: DeviceDescriptor
    # PortAudio index of the stream to open: the capture endpoint itself, or the
    # loopback analogue of a render endpoint. Never exposed or persisted.
    stream_index: int | None
    fmt: AudioFormat | None


class WasapiCaptureHandle:
    def __init__(
        self,
        *,
        pyaudio: ModuleType,
        source: Source,
        meeting_id: UUID,
        epoch: int,
        fmt: AudioFormat,
        frames_per_buffer: int,
        callback: FrameCallback,
        privacy_generation: Callable[[], int],
        clock: Clock,
    ) -> None:
        self._pa = pyaudio
        self.source = source
        self._meeting_id = meeting_id
        self.epoch = epoch
        self.fmt = fmt
        self._callback = callback
        self._privacy_generation = privacy_generation
        self._clock = clock
        self._sample_clock = SampleClock(fmt.sample_rate, frames_per_buffer=frames_per_buffer)
        self._sequence = 0
        self._stream: Any = None
        self._stopping = False
        self._closed = False
        self._close_lock = threading.Lock()
        # Counters read by diagnostics; written only on the callback thread.
        self.callbacks = 0
        self.overflows = 0
        self.malformed = 0
        self.callback_errors = 0
        self.skipped_samples = 0
        self.last_callback_ns: int | None = None

    @property
    def max_device_lead_ns(self) -> int:
        return self._sample_clock.max_device_lead_ns

    def attach(self, stream: Any) -> None:
        self._stream = stream

    def on_audio(self, in_data: bytes | None, frame_count: int, time_info: Any, status: int) -> tuple[None, int]:
        if self._stopping:
            return None, self._pa.paComplete
        try:
            observed = self._clock()
            self.last_callback_ns = observed
            self.callbacks += 1
            channels = self.fmt.channels
            raw = in_data or b""
            if frame_count <= 0 or len(raw) != frame_count * channels * 4:  # float32
                self.malformed += 1
                return None, self._pa.paContinue
            samples = np.frombuffer(raw, dtype=np.float32)
            placement = self._sample_clock.place(frame_count, observed)
            flags = set()
            if status & self._pa.paInputOverflow:
                self.overflows += 1
                flags.add("input_overflow")
            if placement.skipped_samples:
                self.skipped_samples += placement.skipped_samples
                flags.add("discontinuity")
            if placement.device_lead_ns:
                flags.add("clock_lead")
            frame = AudioFrame(
                meeting_id=self._meeting_id,
                source=self.source,
                epoch=self.epoch,
                privacy_generation=self._privacy_generation(),
                sequence=self._sequence,
                first_sample_index=placement.first_sample_index,
                sample_rate=self.fmt.sample_rate,
                channels=channels,
                monotonic_ns=placement.monotonic_ns,
                timing_quality=TimingQuality.CALLBACK,
                data=samples.reshape(frame_count, channels),  # copied by AudioFrame
                flags=frozenset(flags),
            )
            self._sequence += 1
            self._callback(frame)
        except Exception:  # never propagate into PortAudio's thread; counted instead
            self.callback_errors += 1
        return None, self._pa.paContinue

    def is_active(self) -> bool:
        if self._closed or self._stream is None:
            return False
        try:
            return bool(self._stream.is_active())
        except Exception:
            return False

    def stop(self) -> None:
        self._stopping = True
        stream = self._stream
        if stream is not None and not self._closed:
            try:
                stream.stop_stream()  # waits for an in-flight callback to return
            except Exception:
                pass

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self.stop()
            self._closed = True
            if self._stream is not None:
                try:
                    self._stream.close()
                except Exception:
                    pass


class WasapiBackend:
    """CaptureBackend over the Windows WASAPI host API."""

    def __init__(self, *, pyaudio_module: ModuleType | None = None) -> None:
        self._pa_module = pyaudio_module
        self._pa: Any = None
        self._snapshot = 0
        self._endpoints: dict[str, _Endpoint] = {}
        self._handles: list[WasapiCaptureHandle] = []
        # Every public entry point takes this lock exactly once; internal helpers named
        # `*_locked` assume it is held and never re-acquire it (the lock is not reentrant).
        self._lock = threading.Lock()
        self.init_error: str | None = None

    @property
    def module(self) -> ModuleType:
        if self._pa_module is None:
            self._pa_module = _load_pyaudio()
        return self._pa_module

    def _open_handles(self) -> list[WasapiCaptureHandle]:
        self._handles = [h for h in self._handles if not h._closed]
        return self._handles

    def enumerate(self, *, refresh: bool = True) -> list[DeviceDescriptor]:
        """List WASAPI endpoints. PortAudio fixes its device list at initialisation, so a
        refresh restarts it, which is only possible while no stream is open."""
        with self._lock:
            self._refresh_locked(refresh)
            return [e.descriptor for e in self._endpoints.values()]

    def _refresh_locked(self, refresh: bool) -> None:
        if self._pa is not None and (not refresh or self._open_handles()):
            return
        if self._pa is not None:
            self._pa.terminate()
            self._pa = None
        self._endpoints = {}
        try:
            self._pa = self.module.PyAudio()
        except (OSError, RuntimeError):
            self.init_error = AUDIO_SYSTEM_UNAVAILABLE  # empty list; opening reports it
            return
        self.init_error = None
        self._snapshot += 1
        self._endpoints = self._scan()

    def _scan(self) -> dict[str, _Endpoint]:
        pa = self.module
        try:
            host = self._pa.get_host_api_info_by_type(pa.paWASAPI)
        except OSError:
            return {}  # WASAPI unavailable: an empty list, explained by the caller
        infos = list(self._pa.get_device_info_generator_by_host_api(host_api_type=pa.paWASAPI))
        loopbacks: dict[str, list[dict]] = {}
        render_names: dict[str, int] = {}
        for d in infos:
            if d.get("isLoopbackDevice"):
                loopbacks.setdefault(str(d["name"]), []).append(d)
            elif d["maxOutputChannels"] > 0:
                render_names[str(d["name"])] = render_names.get(str(d["name"]), 0) + 1
        found: list[tuple[Direction, str, dict, dict | None, bool, str | None]] = []
        for d in infos:
            if d.get("isLoopbackDevice"):
                continue
            name = str(d["name"])
            if d["maxInputChannels"] > 0:
                found.append((Direction.CAPTURE, name, d, d, d["index"] == host.get("defaultInputDevice"), None))
            if d["maxOutputChannels"] > 0:
                candidates = loopbacks.get(name + LOOPBACK_SUFFIX, [])
                if len(candidates) == 1 and render_names[name] == 1:
                    loop, reason = candidates[0], None
                else:
                    # Several same-named endpoints or loopbacks: which loopback belongs to
                    # which endpoint cannot be proven, so none is assigned.
                    loop, reason = None, AMBIGUOUS_LOOPBACK if candidates else NO_LOOPBACK
                found.append(
                    (Direction.RENDER, name, d, loop, d["index"] == host.get("defaultOutputDevice"), reason)
                )
        counts: dict[str, int] = {}
        for direction, name, *_rest in found:
            key = fingerprint(direction, name)
            counts[key] = counts.get(key, 0) + 1
        endpoints: dict[str, _Endpoint] = {}
        ordinals: dict[str, int] = {}
        for direction, name, info, stream_info, is_default, reason in found:
            base = fingerprint(direction, name)
            device_id = base
            if counts[base] > 1:
                ordinals[base] = ordinals.get(base, 0) + 1
                device_id = f"{base}#{ordinals[base]}@{self._snapshot}"
            fmt = None
            if stream_info is not None and stream_info["maxInputChannels"] > 0:
                fmt = AudioFormat(
                    sample_rate=int(round(stream_info["defaultSampleRate"])),
                    channels=int(stream_info["maxInputChannels"]),
                )
            descriptor = DeviceDescriptor(
                device_id=device_id,
                display_name=name[:256] or "(unnamed endpoint)",
                backend=BACKEND,
                direction=direction,
                identity_stability=IdentityStability.UNSTABLE_FINGERPRINT,
                supports_loopback=direction is Direction.RENDER and fmt is not None,
                default_roles=("default",) if is_default else (),
                probed_formats=(fmt,) if fmt else (),
                available=fmt is not None,
                unavailable_reason=None if fmt is not None else (reason or NO_LOOPBACK),
            )
            endpoints[device_id] = _Endpoint(
                descriptor, None if stream_info is None else int(stream_info["index"]), fmt
            )
        return endpoints

    def resolve(self, device_id: str) -> DeviceDescriptor:
        with self._lock:
            return self._resolve_locked(device_id).descriptor

    def _resolve_locked(self, device_id: str) -> _Endpoint:
        if self._pa is None:
            self._refresh_locked(refresh=True)
        if self._pa is None:
            raise DeviceUnavailable("Windows audio (WASAPI/PortAudio) could not be initialised")
        endpoint = self._endpoints.get(device_id)
        if endpoint is None:
            base = device_id.split("#", 1)[0]
            if "#" in device_id or any(k.startswith(base + "#") for k in self._endpoints):
                raise AmbiguousDevice("duplicate endpoint name; reselect the device")
            raise DeviceUnavailable("selected endpoint is not present")
        return endpoint

    def open(
        self,
        selection: DeviceSelection,
        callback: FrameCallback,
        *,
        meeting_id: UUID,
        epoch: int,
        privacy_generation: Callable[[], int],
        clock: Clock,
    ) -> WasapiCaptureHandle:
        pa = self.module
        with self._lock:
            endpoint = self._resolve_locked(selection.device_id)
            required = _REQUIRED_DIRECTION[selection.source]
            if endpoint.descriptor.direction is not required:
                raise ValueError(f"{selection.source} source needs a {required} endpoint")
            if endpoint.descriptor.unavailable_reason == AMBIGUOUS_LOOPBACK:
                raise AmbiguousDevice(
                    "several playback endpoints share this name; their loopback streams cannot be told apart"
                )
            if endpoint.stream_index is None or endpoint.fmt is None:
                raise DeviceUnavailable("endpoint has no capturable stream (no loopback analogue)")
            fmt = endpoint.fmt
            try:
                self._pa.is_format_supported(
                    fmt.sample_rate,
                    input_device=endpoint.stream_index,
                    input_channels=fmt.channels,
                    input_format=pa.paFloat32,
                )
            except ValueError:
                raise FormatUnsupported("endpoint refused its reported shared-mode format") from None
            frames_per_buffer = max(1, int(fmt.sample_rate * BUFFER_SECONDS))
            handle = WasapiCaptureHandle(
                pyaudio=pa,
                source=selection.source,
                meeting_id=meeting_id,
                epoch=epoch,
                fmt=fmt,
                frames_per_buffer=frames_per_buffer,
                callback=callback,
                privacy_generation=privacy_generation,
                clock=clock,
            )
            try:
                stream = self._pa.open(
                    format=pa.paFloat32,
                    channels=fmt.channels,
                    rate=fmt.sample_rate,
                    input=True,
                    input_device_index=endpoint.stream_index,
                    frames_per_buffer=frames_per_buffer,
                    stream_callback=handle.on_audio,
                    start=False,
                )
            except (OSError, ValueError):
                raise DeviceUnavailable("endpoint could not be opened (in use, removed or denied)") from None
            handle.attach(stream)
            try:
                stream.start_stream()
            except (OSError, ValueError):
                handle.close()
                raise DeviceUnavailable("endpoint could not be started (in use, removed or denied)") from None
            self._handles.append(handle)
            return handle

    def close(self) -> None:
        """Close every stream this backend opened and release PortAudio."""
        with self._lock:
            for handle in self._handles:
                handle.close()
            self._handles = []
            if self._pa is not None:
                self._pa.terminate()
                self._pa = None
            self._endpoints = {}
