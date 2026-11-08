"""The app's speech server cannot outlive the app. A real child process is
placed in the app's kill-on-close job object (speech/qwen.py); when the process that
owns the job ends - here without any cleanup, as in a crash - Windows ends the child.
No socket, model or server is used: the child is a sleeping Python."""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="NOT RUN: Windows job objects only")

OWNER = r"""
import subprocess, sys, os
from meeting_minutes.speech.qwen import _job
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
print(child.pid, _job().assign(child), flush=True)
os._exit(0)  # no cleanup at all: as if the app crashed
"""


def alive(pid: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0) == 0x102  # WAIT_TIMEOUT: still running
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def test_the_server_process_ends_when_the_app_process_ends() -> None:
    owner = subprocess.run([sys.executable, "-c", OWNER], capture_output=True, text=True, timeout=60)
    assert owner.returncode == 0, owner.stderr
    pid, assigned = owner.stdout.split()
    assert assigned == "True"
    deadline = time.monotonic() + 10
    while alive(int(pid)) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not alive(int(pid)), "the child outlived the process that owned it"
