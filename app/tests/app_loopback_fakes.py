"""Stand-ins for Windows' asynchronous audio activation.

`SlowWindows` behaves like `ActivateAudioInterfaceAsync`: it is called through a ctypes
function pointer like the real export, takes a reference to the completion handler,
hands out an operation, and later, on its own thread, calls `ActivateCompleted` and
`Release` through the handler's native vtable, as Windows does. Also used by a
subprocess in the shutdown test, so it imports only the standard library and the app."""

from __future__ import annotations

import ctypes
import sys
import threading
import time

E_NOINTERFACE = -2147467262


class FakeCom:
    """A COM object built in Python (IUnknown plus extra methods)."""

    def __init__(self, extra=()) -> None:
        self.released = 0
        qi = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
        ref = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
        self._fns = [qi(lambda this, riid, ppv: E_NOINTERFACE), ref(lambda this: 2), ref(self._release), *extra]
        self._vtbl = (ctypes.c_void_p * len(self._fns))(*[ctypes.cast(f, ctypes.c_void_p).value for f in self._fns])
        self._object = ctypes.c_void_p(ctypes.addressof(self._vtbl))
        self.pointer = ctypes.addressof(self._object)

    def _release(self, _this) -> int:
        self.released += 1
        return 0


class SlowWindows:
    """`delay` seconds after the call, complete and let go of the handler; `delay=None`
    never completes (Windows keeps its reference). `say` prints a marker when the
    callback is about to enter the handler (for the subprocess check)."""

    def __init__(self, delay: float | None, activation_hr: int = 0, call_hr: int = 0, say: bool = False) -> None:
        self.delay, self.activation_hr, self.call_hr, self.say = delay, activation_hr, call_hr, say
        self.client = FakeCom()
        result = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_long),
                                    ctypes.POINTER(ctypes.c_void_p))
        self.operation = FakeCom([result(self._result)])
        self.completed = threading.Event()
        self.calls = 0
        proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.POINTER(ctypes.c_void_p))
        self._native = proto(self._activate)

    def __call__(self, *args) -> int:
        return self._native(*args)

    def _result(self, _this, hr, unknown) -> int:
        hr[0] = self.activation_hr
        unknown[0] = self.client.pointer if self.activation_hr >= 0 else None
        return self.call_hr

    def _activate(self, path, iid, variant, handler, operation_out) -> int:
        from meeting_minutes.capture import app_loopback as al

        assert path.endswith("Process_Loopback")
        self.calls += 1
        al._method(handler, 1, ctypes.c_ulong)()  # Windows keeps a reference
        operation_out[0] = self.operation.pointer
        if self.delay is None:
            return 0

        def complete() -> None:
            time.sleep(self.delay)
            if self.say:
                print("callback entering handler", flush=True)
            al._method(handler, 3, ctypes.c_long, ctypes.c_void_p)(self.operation.pointer)  # ActivateCompleted
            al._method(handler, 2, ctypes.c_ulong)()  # and lets go of the handler
            self.completed.set()

        threading.Thread(target=complete, daemon=True).start()
        return 0


SHUTDOWN_SCRIPT = r"""
import atexit, sys
atexit.register(lambda: print("normal teardown continued", flush=True))  # runs after the guard
sys.path.insert(0, sys.argv[3])
from meeting_minutes.capture import app_loopback as al
from meeting_minutes.capture.base import DeviceUnavailable
from app_loopback_fakes import SlowWindows
al.EXIT_WAIT_S = float(sys.argv[2])
delay = None if sys.argv[1] == "never" else float(sys.argv[1])
windows = SlowWindows(delay, say=True)
try:
    al._activate(1234, timeout=0.05, activate=windows)
except DeviceUnavailable as exc:
    print("start gave up:", exc, flush=True)
al.note_exit_code(7)
raise SystemExit(7)
"""


def run_shutdown(delay: str, grace: float, tests_dir: str):
    import subprocess

    return subprocess.run([sys.executable, "-c", SHUTDOWN_SCRIPT, delay, str(grace), tests_dir],
                          capture_output=True, text=True, timeout=60)
