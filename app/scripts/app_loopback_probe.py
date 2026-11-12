"""Developer probe for application audio capture; not part of the app.

Starts two ordinary child programs (Python tone players) that play different quiet
in-memory test tones on the default output at the same time, then captures each
program with Windows process loopback through the app's backend and measures, at the
capture boundary, how much of each tone arrives. Also observes: packets while the
target is silent, a target that exits during capture, and reopening a closed target.

Prints numbers only (JSON). No microphone is opened and no audio is written anywhere.

    .\\.venv\\Scripts\\python.exe scripts\\app_loopback_probe.py [--seconds 3] [--dbfs -30]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
import uuid

import numpy as np

from meeting_minutes.capture.app_loopback import (
    AppLoopbackBackend,
    device_id_for,
    process_created,
)
from meeting_minutes.capture.base import DeviceUnavailable
from meeting_minutes.domain.contracts import DeviceSelection, Source

PLAYER = r"""
import sys, time, numpy as np, pyaudiowpatch as pa
freq, amp, seconds, delay = float(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
p = pa.PyAudio()
info = p.get_default_output_device_info()
rate = int(info["defaultSampleRate"])
s = p.open(format=pa.paFloat32, channels=1, rate=rate, output=True)
print("ready", flush=True)
if delay > 0:
    s.write(np.zeros(int(rate * delay), dtype=np.float32).tobytes())
t = np.arange(int(rate * seconds)) / rate
s.write((amp * np.sin(2 * np.pi * freq * t)).astype(np.float32).tobytes())
s.stop_stream(); s.close(); p.terminate()
"""


def player(freq: float, amp: float, seconds: float, delay: float = 0.0) -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, "-c", PLAYER, str(freq), str(amp), str(seconds), str(delay)],
                            stdout=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    proc.stdout.readline()  # "ready": the output stream is open
    return proc


def tone_dbfs(samples: np.ndarray, rate: int, freq: float) -> float:
    """Level of one frequency (Goertzel-like single DFT bin over the whole capture)."""
    if samples.size == 0:
        return float("-inf")
    mono = samples.mean(axis=1) if samples.ndim == 2 else samples
    t = np.arange(mono.size) / rate
    amplitude = 2 * abs(np.dot(mono, np.exp(-2j * np.pi * freq * t))) / mono.size
    return round(20 * np.log10(max(amplitude, 1e-12)), 1)


def capture(backend: AppLoopbackBackend, pid: int, seconds: float, *, kill: subprocess.Popen | None = None,
            kill_after: float = 0.0) -> dict:
    frames: list[np.ndarray] = []
    lock = threading.Lock()

    def on_frame(frame) -> None:
        with lock:
            frames.append(np.array(frame.data))

    created = process_created(pid)
    started = time.monotonic()
    try:
        handle = backend.open(DeviceSelection(source=Source.SYSTEM, device_id=device_id_for(pid, created or 0)),
                              on_frame, meeting_id=uuid.uuid4(), epoch=1, privacy_generation=lambda: 0,
                              clock=time.monotonic_ns)
    except (LookupError, ValueError, OSError) as exc:
        return {"opened": False, "error": type(exc).__name__, "detail": str(exc)}
    open_ms = round((time.monotonic() - started) * 1000)
    active_after_exit = None
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(0.05)
        if kill is not None and time.monotonic() - started > kill_after and kill.poll() is None:
            kill.kill()
            kill.wait()
            time.sleep(0.3)
            active_after_exit = handle.is_active()
    stats = {"packets": handle.packets, "silent_packets": handle.silent_packets,
             "discontinuities": handle.discontinuities, "callback_errors": handle.callback_errors,
             "skipped_samples": handle.skipped_samples, "active_at_end": handle.is_active()}
    handle.close()
    with lock:
        data = np.concatenate(frames) if frames else np.zeros((0, 2), dtype=np.float32)
    stats.update(opened=True, open_ms=open_ms, seconds_received=round(data.shape[0] / 48_000, 2),
                 peak_dbfs=round(20 * np.log10(max(float(np.abs(data).max()) if data.size else 0, 1e-12)), 1),
                 active_after_exit=active_after_exit)
    stats["data"] = data
    return stats


class _Collect:
    def __init__(self) -> None:
        self.frames: list[np.ndarray] = []

    def accept(self, frame) -> None:
        self.frames.append(np.array(frame.data))

    def reset(self, source) -> int:
        return 0


def controller_run(pid: int, seconds: float, tone_a: float, tone_b: float) -> dict:
    from meeting_minutes.capture.app_loopback import RoutedBackend
    from meeting_minutes.capture.controller import CaptureController, CaptureWorker
    from meeting_minutes.capture.wasapi import WasapiBackend

    sink = _Collect()
    controller = CaptureController(RoutedBackend(WasapiBackend(), AppLoopbackBackend()), mic_included=False,
                                   sink=sink)
    worker = CaptureWorker(controller)
    started = controller.start(mic=None, system=device_id_for(pid, process_created(pid) or 0))
    worker.start()
    time.sleep(seconds)
    status = controller.status()["sources"]["system"]
    stop = controller.stop()
    worker.stop()
    controller.close()
    data = np.concatenate(sink.frames) if sink.frames else np.zeros((0, 2), dtype=np.float32)
    return {"state": started.state.value, "frames_forwarded": status["frames_forwarded"],
            "mic_state": controller.source_state(Source.MIC).value,
            "gaps": [(g.reason.value, g.end_ms - g.start_ms) for g in controller.gaps],
            "drain_completed": stop.drain_completed, "seconds_forwarded": round(data.shape[0] / 48_000, 2),
            "tone_a_dbfs": tone_dbfs(data, 48_000, tone_a), "tone_b_dbfs": tone_dbfs(data, 48_000, tone_b)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--dbfs", type=float, default=-30.0)
    args = parser.parse_args()
    amp = 10 ** (args.dbfs / 20)
    tone_a, tone_b = 500.0, 1300.0
    backend = AppLoopbackBackend()
    report: dict = {"supported": backend.supported(), "tone_a_hz": tone_a, "tone_b_hz": tone_b,
                    "tone_dbfs": args.dbfs}

    a = player(tone_a, amp, args.seconds + 3)
    b = player(tone_b, amp, args.seconds + 3)
    try:
        for name, proc in (("capture_a", a), ("capture_b", b)):
            result = capture(backend, proc.pid, args.seconds)
            data = result.pop("data", None)
            if data is not None:
                result["tone_a_dbfs"] = tone_dbfs(data, 48_000, tone_a)
                result["tone_b_dbfs"] = tone_dbfs(data, 48_000, tone_b)
            report[name] = result
    finally:
        for proc in (a, b):
            proc.kill()
            proc.wait()

    # The same isolation after the meeting's capture controller (privacy boundary, bounded
    # queues), with no microphone selected: what the speech pipeline would receive.
    a = player(tone_a, amp, args.seconds + 3)
    b = player(tone_b, amp, args.seconds + 3)
    try:
        report["controller_a"] = controller_run(a.pid, args.seconds, tone_a, tone_b)
    finally:
        for proc in (a, b):
            proc.kill()
            proc.wait()

    # A program with an open output stream that is silent for the first seconds.
    quiet = player(tone_a, amp, 2.0, delay=args.seconds)
    try:
        result = capture(backend, quiet.pid, args.seconds)
        result.pop("data", None)
        report["silent_target"] = result
    finally:
        quiet.kill()
        quiet.wait()

    # A target that exits during capture, then an attempt to reopen it.
    leaving = player(tone_b, amp, 10.0)
    pid, created = leaving.pid, process_created(leaving.pid)
    result = capture(backend, pid, 2.0, kill=leaving, kill_after=1.0)
    result.pop("data", None)
    report["target_exits"] = result
    try:
        backend.open(DeviceSelection(source=Source.SYSTEM, device_id=device_id_for(pid, created or 0)),
                     lambda f: None, meeting_id=uuid.uuid4(), epoch=1, privacy_generation=lambda: 0,
                     clock=time.monotonic_ns)
        report["reopen_closed_target"] = "opened (unexpected)"
    except DeviceUnavailable as exc:
        report["reopen_closed_target"] = f"DeviceUnavailable: {exc}"
    backend.close()
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
