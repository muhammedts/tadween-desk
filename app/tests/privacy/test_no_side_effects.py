"""Runtime audit of doctor and the fake backend in a fresh interpreter.

A CPython audit hook records every file open, socket and subprocess event while
`doctor --no-capture` and two seconds of fake two-source capture run, and while
the full `capture-test` command runs the controller, privacy gate, worker and
mute/unmute on the real-time fake backend. Bytecode writing is disabled (-B) so
interpreter caching does not mask application writes.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

AUDIT_SCRIPT = textwrap.dedent(
    r"""
    import io, json, os, sys
    from contextlib import redirect_stdout
    from uuid import UUID

    from meeting_minutes.cli import main
    from meeting_minutes.capture.fake import FakeAudioBackend, pump_until
    from meeting_minutes.domain.contracts import DeviceSelection, Source

    WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
    events = {"writes": [], "sockets": [], "subprocesses": []}

    def hook(name, args):
        if name == "open":
            path, mode, flags = args
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & WRITE_FLAGS
            )
            if writing:
                events["writes"].append(str(path))
        elif name.startswith("socket."):
            events["sockets"].append(name)
        elif name == "subprocess.Popen":
            # args = (executable, args, cwd, env); on Windows args is a command-line string.
            argv = args[1] if isinstance(args[1], (list, tuple)) else str(args[1]).split()
            events["subprocesses"].append(os.path.basename(str(argv[0])).lower() if argv else "")

    sys.addaudithook(hook)

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = main(["doctor", "--no-capture", "--json"])
    frames = []
    backend = FakeAudioBackend(meeting_id=UUID(int=1))
    handles = [
        backend.open(DeviceSelection(source=Source.MIC, device_id="fake:mic-48k-mono"), frames.append),
        backend.open(DeviceSelection(source=Source.SYSTEM, device_id="fake:system-44k1-stereo"), frames.append),
    ]
    pump_until(handles, 2 * 10**9)

    forbidden = {"pyaudiowpatch", "pyaudio", "sounddevice", "soundcard", "PySide6",
                 "sherpa_onnx", "onnxruntime", "soxr", "docx", "requests", "httpx", "urllib3"}
    loaded = sorted(m for m in sys.modules if m.split(".")[0] in forbidden)
    sys.stdout.write(json.dumps({"exit": code, "frames": len(frames), "loaded": loaded, **events}))
    """
)


def run_audit() -> dict:
    proc = subprocess.run(
        [sys.executable, "-B", "-c", AUDIT_SCRIPT],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_doctor_and_fake_capture_write_nothing_and_open_no_sockets():
    result = run_audit()
    assert result["exit"] == 0
    assert result["frames"] > 0
    assert result["writes"] == []
    assert result["sockets"] == []
    assert result["loaded"] == []
    # The only child processes are the read-only git revision/status queries.
    assert set(result["subprocesses"]) <= {"git", "git.exe"}


CAPTURE_SCRIPT = AUDIT_SCRIPT.split("buffer = io.StringIO()")[0] + textwrap.dedent(
    r"""
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = main(["capture-test", "--backend", "fake", "--mic", "fake:mic-48k-mono",
                     "--system", "fake:system-44k1-stereo", "--seconds", "1.2",
                     "--exclude-mic-at", "0.4", "--include-mic-at", "0.8", "--json"])
    report = json.loads(buffer.getvalue())
    forbidden = {"pyaudiowpatch", "pyaudio", "sounddevice", "soundcard", "PySide6",
                 "sherpa_onnx", "onnxruntime", "soxr", "docx", "requests", "httpx", "urllib3"}
    loaded = sorted(m for m in sys.modules if m.split(".")[0] in forbidden)
    frames = sum(s["frames_forwarded"] for s in report["status"]["sources"].values())
    sys.stdout.write(json.dumps({"exit": code, "frames": frames, "loaded": loaded, **events}))
    """
)


def test_capture_controller_and_mute_write_nothing_and_open_no_sockets():
    proc = subprocess.run(
        [sys.executable, "-B", "-c", CAPTURE_SCRIPT], capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["exit"] == 0
    assert result["frames"] > 100
    assert result["writes"] == []
    assert result["sockets"] == []
    assert result["subprocesses"] == []
    assert result["loaded"] == []
