"""Windows application audio capture through process loopback.

Captures the audio one application renders, independent of the output device, with
the documented Windows process-loopback activation: `ActivateAudioInterfaceAsync` on
`VAD\\Process_Loopback` with `AUDIOCLIENT_ACTIVATION_PARAMS` naming a process and
`PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE`. Windows 10 build 20348 or later.
Only the standard library (`ctypes`) is used; no new dependency.

Scope of a capture: the chosen process and its child processes. A browser's tabs and
windows usually share one process tree, so their audio is captured together.

Identity: an application source is `app:<pid>:<creation time>`, the top-level process
of a program that has a visible window. The creation time makes a reused process id a
different source, so Start and Reconnect never attach to another process: a closed
application stops resolving (`DeviceUnavailable`) and the user chooses again. The
display name is the program's own description (or its file name); window titles are
offered separately for telling instances apart on screen and are never part of the
descriptor, so they are not stored with the meeting.

The capture thread copies each packet into an owned `AudioFrame` with a sample
position from a `SampleClock`, exactly like the WASAPI backend, and hands it to the
controller's gate. Nothing is written to disk. The format is fixed (48 kHz stereo
float32, converted by Windows), because process loopback has no mix format of its own.
"""

from __future__ import annotations

import atexit
import ctypes
import os
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import numpy as np

from meeting_minutes.capture.base import (
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

BACKEND = "app_loopback"
ID_PREFIX = "app:"
MIN_WINDOWS_BUILD = 20348
FORMAT = AudioFormat(sample_rate=48_000, channels=2)
PACKET_FRAMES = 480  # 10 ms; only sizes the clock's jitter allowance
OPEN_TIMEOUT_S = 5.0
WAIT_MS = 50
NAME_LIMIT = 80

UNSUPPORTED = "process_loopback_unsupported"
BUSY = "process_loopback_busy"  # too many activations still held by Windows
APPLICATION_CLOSED = "application_closed"


def is_application(descriptor: DeviceDescriptor) -> bool:
    """True for an application source (this backend, or a test double declaring it)."""
    return descriptor.backend == BACKEND


def is_application_id(device_id: str | None) -> bool:
    return bool(device_id) and device_id.startswith(ID_PREFIX)


def device_id_for(pid: int, created: int) -> str:
    return f"{ID_PREFIX}{pid}:{created}"


def parse_device_id(device_id: str) -> tuple[int, int]:
    if not is_application_id(device_id):
        raise DeviceUnavailable("not an application source")
    try:
        pid_text, created_text = device_id[len(ID_PREFIX):].split(":", 1)
        return int(pid_text), int(created_text)
    except ValueError:
        raise DeviceUnavailable("malformed application source") from None


# --------------------------------------------------------------- pure grouping


@dataclass(frozen=True, slots=True)
class ProcessInfo:
    pid: int
    parent_pid: int
    exe: str
    created: int | None  # FILETIME ticks; None if the process could not be queried


@dataclass(frozen=True, slots=True)
class Application:
    pid: int
    created: int
    exe: str
    titles: tuple[str, ...]

    @property
    def device_id(self) -> str:
        return device_id_for(self.pid, self.created)


def top_process(pid: int, processes: dict[int, ProcessInfo]) -> ProcessInfo | None:
    """Walk up while the parent runs the same program and is older than its child
    (an older parent rules out a reused parent id). The result is the process whose
    tree holds the program's helper processes, such as a browser's audio service."""
    current = processes.get(pid)
    seen: set[int] = set()
    while current is not None and current.pid not in seen:
        seen.add(current.pid)
        parent = processes.get(current.parent_pid)
        if (
            parent is None
            or parent.pid == current.pid
            or parent.exe.lower() != current.exe.lower()
            or parent.created is None
            or current.created is None
            or parent.created > current.created
        ):
            break
        current = parent
    return current


def group_applications(
    processes: dict[int, ProcessInfo], windows: Iterable[tuple[int, str]], *, exclude_pids: Iterable[int] = ()
) -> list[Application]:
    """Programs with a visible window, one entry per top-level process."""
    excluded = set(exclude_pids)
    titles: dict[int, list[str]] = {}
    for pid, title in windows:
        top = top_process(pid, processes)
        if top is None or top.created is None or top.pid in excluded or pid in excluded:
            continue
        titles.setdefault(top.pid, [])
        if title and title not in titles[top.pid]:
            titles[top.pid].append(title)
    apps = [
        Application(pid=pid, created=processes[pid].created or 0, exe=processes[pid].exe, titles=tuple(t))
        for pid, t in titles.items()
    ]
    return sorted(apps, key=lambda a: (a.exe.lower(), a.pid))


def clean_name(text: str | None, fallback: str) -> str:
    name = " ".join((text or "").split())[:NAME_LIMIT]
    if not name:
        stem = fallback.rsplit(".", 1)[0] if fallback.lower().endswith(".exe") else fallback
        name = " ".join(stem.split())[:NAME_LIMIT] or "(unnamed program)"
    return name


# ------------------------------------------------------------------ Win32 layer

if sys.platform == "win32":
    from ctypes import wintypes

    _HRESULT = ctypes.c_long

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                    ("Data4", ctypes.c_ubyte * 8)]

    def _guid(text: str) -> GUID:
        return GUID.from_buffer_copy(uuid.UUID(text).bytes_le)

    IID_IUnknown = _guid("00000000-0000-0000-C000-000000000046")
    IID_IAgileObject = _guid("94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90")
    IID_IActivateCompletion = _guid("41D949AB-9862-444A-80F6-C261334DA5EB")
    IID_IAudioClient = _guid("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2")
    IID_IAudioCaptureClient = _guid("C8ADBD64-E71E-48A0-A4DE-185C395CD317")

    class _BLOB(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("pBlobData", ctypes.c_void_p)]

    class _PROPVARIANT(ctypes.Structure):
        _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                    ("r3", ctypes.c_ushort), ("blob", _BLOB)]

    class _ActivationParams(ctypes.Structure):
        _fields_ = [("ActivationType", ctypes.c_uint), ("TargetProcessId", wintypes.DWORD),
                    ("ProcessLoopbackMode", ctypes.c_uint)]

    class _WAVEFORMATEX(ctypes.Structure):
        _pack_ = 2
        _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD),
                    ("nSamplesPerSec", wintypes.DWORD), ("nAvgBytesPerSec", wintypes.DWORD),
                    ("nBlockAlign", wintypes.WORD), ("wBitsPerSample", wintypes.WORD), ("cbSize", wintypes.WORD)]

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    class _OSVERSIONINFOW(ctypes.Structure):
        _fields_ = [("dwOSVersionInfoSize", wintypes.DWORD), ("dwMajorVersion", wintypes.DWORD),
                    ("dwMinorVersion", wintypes.DWORD), ("dwBuildNumber", wintypes.DWORD),
                    ("dwPlatformId", wintypes.DWORD), ("szCSDVersion", ctypes.c_wchar * 128)]

    _QI = ctypes.WINFUNCTYPE(_HRESULT, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
    _REF = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
    _COMPLETED = ctypes.WINFUNCTYPE(_HRESULT, ctypes.c_void_p, ctypes.c_void_p)

    class _HandlerVtbl(ctypes.Structure):
        _fields_ = [("QueryInterface", _QI), ("AddRef", _REF), ("Release", _REF), ("ActivateCompleted", _COMPLETED)]

    class _HandlerObject(ctypes.Structure):
        _fields_ = [("lpVtbl", ctypes.POINTER(_HandlerVtbl))]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _ole32 = ctypes.WinDLL("ole32")

    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    _kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                     ctypes.POINTER(wintypes.DWORD)]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.CreateEventW.restype = wintypes.HANDLE
    _kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, ctypes.c_uint]

    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.GetWindow.restype = wintypes.HWND
    _user32.GetWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]

    _ole32.CoInitializeEx.restype = _HRESULT
    _ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    _ole32.CoUninitialize.argtypes = []

    TH32CS_SNAPPROCESS = 0x2
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    SYNCHRONIZE = 0x00100000
    WAIT_OBJECT_0 = 0
    INVALID_HANDLE = ctypes.c_void_p(-1).value
    GW_OWNER = 4
    S_OK = 0
    E_NOINTERFACE = -2147467262  # 0x80004002
    VT_BLOB = 65
    PROCESS_LOOPBACK = 1
    INCLUDE_TARGET_PROCESS_TREE = 0
    AUDCLNT_SHAREMODE_SHARED = 0
    AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
    AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
    AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
    AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
    AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY = 0x1
    AUDCLNT_BUFFERFLAGS_SILENT = 0x2
    WAVE_FORMAT_IEEE_FLOAT = 3
    BUFFER_HNS = 2_000_000  # 200 ms shared buffer

    def windows_build() -> int:
        info = _OSVERSIONINFOW()
        info.dwOSVersionInfoSize = ctypes.sizeof(info)
        if ctypes.WinDLL("ntdll").RtlGetVersion(ctypes.byref(info)) != 0:
            return 0
        return int(info.dwBuildNumber)

    def _filetime(ft: wintypes.FILETIME) -> int:
        return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

    def _process_times(handle: int) -> int | None:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not _kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        return _filetime(times[0])

    def _image_path(handle: int) -> str | None:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return buffer.value

    def process_created(pid: int) -> int | None:
        handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            return _process_times(handle)
        finally:
            _kernel32.CloseHandle(handle)

    def list_processes() -> dict[int, ProcessInfo]:
        snapshot = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snapshot or snapshot == INVALID_HANDLE:
            return {}
        raw: list[tuple[int, int, str]] = []
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            ok = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                raw.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID), entry.szExeFile))
                ok = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            _kernel32.CloseHandle(snapshot)
        return {pid: ProcessInfo(pid, ppid, exe, process_created(pid) if pid else None) for pid, ppid, exe in raw}

    def _cloaked(hwnd: int) -> bool:
        value = ctypes.c_int(0)
        try:
            result = ctypes.WinDLL("dwmapi").DwmGetWindowAttribute(
                wintypes.HWND(hwnd), 14, ctypes.byref(value), ctypes.sizeof(value))  # DWMWA_CLOAKED
        except OSError:
            return False
        return result == 0 and value.value != 0

    def list_windows() -> list[tuple[int, str]]:
        """(process id, title) of visible, unowned, titled top-level windows."""
        found: list[tuple[int, str]] = []

        def visit(hwnd: int, _lparam: int) -> bool:
            try:
                if not _user32.IsWindowVisible(hwnd) or _user32.GetWindow(hwnd, GW_OWNER):
                    return True
                length = _user32.GetWindowTextLengthW(hwnd)
                if length <= 0 or _cloaked(hwnd):
                    return True
                buffer = ctypes.create_unicode_buffer(length + 1)
                _user32.GetWindowTextW(hwnd, buffer, length + 1)
                pid = wintypes.DWORD(0)
                _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value and buffer.value.strip():
                    found.append((int(pid.value), " ".join(buffer.value.split())[:200]))
            except Exception:  # noqa: BLE001 - one odd window never stops the listing
                pass
            return True

        _user32.EnumWindows(_WNDENUMPROC(visit), 0)
        return found

    def program_description(pid: int) -> str | None:
        """The program's own FileDescription (for example "Microsoft Teams")."""
        handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            path = _image_path(handle)
        finally:
            _kernel32.CloseHandle(handle)
        if not path:
            return None
        try:
            version = ctypes.WinDLL("version")
            size = version.GetFileVersionInfoSizeW(path, None)
            if not size:
                return None
            data = ctypes.create_string_buffer(size)
            if not version.GetFileVersionInfoW(path, 0, size, data):
                return None
            pointer, length = ctypes.c_void_p(), ctypes.c_uint(0)
            if not version.VerQueryValueW(data, "\\VarFileInfo\\Translation", ctypes.byref(pointer),
                                          ctypes.byref(length)) or length.value < 4:
                return None
            lang, codepage = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ushort * 2)).contents
            key = f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\FileDescription"
            if not version.VerQueryValueW(data, key, ctypes.byref(pointer), ctypes.byref(length)) or not length.value:
                return None
            return ctypes.wstring_at(pointer.value, length.value).split("\0", 1)[0]
        except OSError:
            return None

    def _method(obj: int, index: int, restype: Any, *argtypes: Any) -> Callable[..., Any]:
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        bound = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[index])
        return lambda *args: bound(obj, *args)

    def _release(obj: int | None) -> None:
        if obj:
            try:
                _method(obj, 2, ctypes.c_ulong)()
            except OSError:
                pass

    # Activation handlers. Windows holds a pointer to the completion
    # handler from ActivateAudioInterfaceAsync until it releases it, possibly after Tadween
    # stopped waiting. So nothing Windows can reach is ever freed while Python runs:
    # - the four COM entry points are shared module-level thunks, created once;
    # - handler objects live in a fixed pool of MAX_PENDING slots, allocated once.
    # Per-request state is a plain Python object found by the slot's address; a slot is
    # reused once Windows and Tadween have both let go of it (reclaimed). A request
    # Windows never finishes keeps its slot, so pending requests are bounded across
    # retries. At exit, if Windows still holds a slot after a short grace period, the
    # process ends before interpreter teardown can begin (`_exit_guard`).
    MAX_PENDING = 8
    EXIT_WAIT_S = 2.0
    _STATE: dict[int, _Activation] = {}  # slot address -> request using it
    _STATE_LOCK = threading.Lock()
    _EXIT = {"code": 0}

    def _entry(this: Any) -> _Activation | None:
        return _STATE.get(int(this or 0))  # one dict read: atomic under the GIL

    def _qi_thunk(this: int, riid: Any, ppv: Any) -> int:
        activation = _entry(this)
        if activation is None:
            ppv[0] = None
            return E_NOINTERFACE
        return activation.query(this, riid, ppv)

    def _add_ref_thunk(this: int) -> int:
        activation = _entry(this)
        return activation.add_ref() if activation is not None else 1

    def _release_thunk(this: int) -> int:
        activation = _entry(this)
        return activation.drop_ref() if activation is not None else 0

    def _completed_thunk(this: int, _operation: int) -> int:
        activation = _entry(this)
        if activation is not None:
            try:
                activation.completed()
            except Exception:  # noqa: BLE001 - never raise into Windows' thread
                pass
        return S_OK

    _VTBL = _HandlerVtbl(_QI(_qi_thunk), _REF(_add_ref_thunk), _REF(_release_thunk), _COMPLETED(_completed_thunk))
    _SLOTS = (_HandlerObject * MAX_PENDING)()
    for _slot in _SLOTS:
        _slot.lpVtbl = ctypes.pointer(_VTBL)
    _SLOT_ADDRESSES = tuple(ctypes.addressof(_slot) for _slot in _SLOTS)

    class _Activation:
        """One ActivateAudioInterfaceAsync request in a handler slot: a minimal
        free-threaded IActivateAudioInterfaceCompletionHandler (also IAgileObject, which
        activation requires) with real reference counting, plus the operation.

        Tadween holds one reference and the operation until it has taken the result
        (`result`) or given up (`abandon`). A completion after the caller gave up is
        cleaned up on Windows' thread: the audio client is released unused and the
        operation released. The operation is released exactly once on every path. When
        the count reaches zero no native call can reach the slot, and it is reclaimed."""

        _accepted = (bytes(IID_IUnknown), bytes(IID_IAgileObject), bytes(IID_IActivateCompletion))

        def __init__(self, slot: int) -> None:
            self.slot = slot
            self._lock = threading.Lock()
            self.done = threading.Event()
            self.refs = 1  # Tadween's own reference
            self.abandoned = False
            self.finished = False
            self.reclaimed = False
            self.late_clients_released = 0
            self.operation = ctypes.c_void_p()
            self.keep: tuple[Any, ...] = ()  # activation parameters, alive for the call

        @property
        def pointer(self) -> int:
            return self.slot

        # -- COM side (any thread, including Windows' own)

        def query(self, this: int, riid: Any, ppv: Any) -> int:
            if bytes(riid.contents) in self._accepted:
                ppv[0] = this
                self.add_ref()  # COM: a successful QueryInterface adds a reference
                return S_OK
            ppv[0] = None
            return E_NOINTERFACE

        def add_ref(self) -> int:
            with self._lock:
                self.refs += 1
                return self.refs

        def drop_ref(self) -> int:
            with self._lock:
                self.refs = max(0, self.refs - 1)
                refs = self.refs
            if refs == 0:
                self._reclaim()
            return refs

        def completed(self) -> None:
            with self._lock:
                self.done.set()
                late = self.abandoned
            if late:
                self._discard_late()

        # -- Tadween's side

        def abandon(self) -> bool:
            """Stop waiting. True: the completion will clean up when it comes. False: it
            has already completed, so the caller must still take `result()`."""
            with self._lock:
                if self.done.is_set():
                    return False
                self.abandoned = True
                return True

        def _take(self) -> tuple[int, int, int]:
            """(call result, activation result, client pointer) from the operation."""
            hr, unknown = _HRESULT(0), ctypes.c_void_p()
            call = _method(self.operation.value, 3, _HRESULT, ctypes.POINTER(_HRESULT),
                           ctypes.POINTER(ctypes.c_void_p))(ctypes.byref(hr), ctypes.byref(unknown))
            return int(call), int(hr.value), int(unknown.value or 0)

        def result(self) -> int:
            """The activated IAudioClient (caller owns it); raises if activation failed."""
            try:
                call, hr, client = self._take()
                if call < 0 or hr < 0 or not client:
                    _release(client or None)
                    code = call if call < 0 else hr
                    raise DeviceUnavailable(f"process loopback activation failed (0x{code & 0xFFFFFFFF:08x})")
                return client
            finally:
                self.finish()

        def _discard_late(self) -> None:
            try:
                _call, _hr, client = self._take()
                if client:
                    _release(client)
                    self.late_clients_released += 1
            finally:
                self.finish()

        def finish(self) -> None:
            """Release the operation and Tadween's own reference (once)."""
            with self._lock:
                if self.finished:
                    return
                self.finished = True
                operation, self.operation = self.operation.value, ctypes.c_void_p()
            _release(operation or None)
            self.drop_ref()

        def _reclaim(self) -> None:
            with _STATE_LOCK:
                if _STATE.get(self.slot) is self:
                    del _STATE[self.slot]
                    self.reclaimed = True
                    self.keep = ()

    def _allocate() -> _Activation:
        """A free handler slot, or DeviceUnavailable when MAX_PENDING requests are still
        held by Windows (bounded across retries; nothing grows)."""
        with _STATE_LOCK:
            for slot in _SLOT_ADDRESSES:
                if slot not in _STATE:
                    activation = _Activation(slot)
                    _STATE[slot] = activation
                    return activation
        raise DeviceUnavailable(BUSY)

    def pending_activations() -> int:
        """Handler slots Windows or Tadween still holds."""
        return len(_STATE)

    def note_exit_code(code: int) -> None:
        """The exit code to use if `_exit_guard` has to end the process itself."""
        _EXIT["code"] = int(code) if isinstance(code, int) else 1

    def _exit_guard() -> None:
        """At exit (atexit runs before interpreter teardown starts): wait up to
        EXIT_WAIT_S for Windows to release every handler. If one is still held, Windows
        may call it at any moment, including during or after teardown, so the process
        ends here with the intended exit code and teardown never begins. Normally nothing
        is pending and exit continues as usual."""
        deadline = time.monotonic() + EXIT_WAIT_S
        while _STATE and time.monotonic() < deadline:
            time.sleep(0.02)
        if not _STATE:
            return
        try:
            _last_words()
        finally:  # nothing above may keep the process from ending here
            _kernel32.TerminateProcess(_kernel32.GetCurrentProcess(), _EXIT["code"] & 0xFFFFFFFF)

    def _last_words() -> None:
        """Say why, and flush logging and output, before `_exit_guard` ends the process.
        Best effort, one step at a time: a windowed start (pythonw, the CMD launcher) has
        no stdout or stderr at all, and a stream can be closed or fail."""
        steps: tuple[Callable[[], Any], ...] = (
            lambda: sys.stderr.write("Tadween: Windows still holds an audio activation; ending without "
                                     "interpreter teardown.\n"),
            lambda: sys.modules["logging"].shutdown() if "logging" in sys.modules else None,
            lambda: sys.stdout.flush(),
            lambda: sys.stderr.flush(),
        )
        for step in steps:
            try:
                step()
            except Exception:  # noqa: BLE001 - missing, closed or failing stream
                pass

    atexit.register(_exit_guard)

    def _activate_audio_interface(*args: Any) -> int:
        activate = ctypes.WinDLL("Mmdevapi").ActivateAudioInterfaceAsync
        activate.restype = _HRESULT
        activate.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(GUID), ctypes.POINTER(_PROPVARIANT), ctypes.c_void_p,
                             ctypes.POINTER(ctypes.c_void_p)]
        return int(activate(*args))

    def _activate(pid: int, *, timeout: float = OPEN_TIMEOUT_S,
                  activate: Callable[..., int] = _activate_audio_interface) -> int:
        """Activate an IAudioClient for the process tree of `pid` (COM MTA thread).
        Bounded by `timeout`; a later completion is cleaned up without capturing."""
        params = _ActivationParams(PROCESS_LOOPBACK, pid, INCLUDE_TARGET_PROCESS_TREE)
        variant = _PROPVARIANT()
        variant.vt = VT_BLOB
        variant.blob.cbSize = ctypes.sizeof(params)
        variant.blob.pBlobData = ctypes.addressof(params)
        activation = _allocate()
        activation.keep = (params, variant)
        hr = activate("VAD\\Process_Loopback", ctypes.byref(IID_IAudioClient), ctypes.byref(variant),
                      activation.pointer, ctypes.byref(activation.operation))
        if hr < 0:
            activation.finish()  # releases an operation if one was returned anyway
            raise DeviceUnavailable(f"process loopback activation refused (0x{hr & 0xFFFFFFFF:08x})")
        if not activation.done.wait(timeout) and activation.abandon():
            raise DeviceUnavailable("process loopback activation timed out")
        return activation.result()
else:  # pragma: no cover - the backend is Windows-only; pure helpers stay importable

    def windows_build() -> int:
        return 0

    def list_processes() -> dict[int, ProcessInfo]:
        return {}

    def list_windows() -> list[tuple[int, str]]:
        return []

    def program_description(pid: int) -> str | None:
        return None

    def process_created(pid: int) -> int | None:
        return None

    def pending_activations() -> int:
        return 0

    def note_exit_code(code: int) -> None:
        return None


# --------------------------------------------------------------------- handle


class AppLoopbackHandle:
    """One process-loopback stream on its own COM (MTA) thread."""

    def __init__(
        self,
        *,
        pid: int,
        created: int,
        source: Source,
        meeting_id: UUID,
        epoch: int,
        callback: FrameCallback,
        privacy_generation: Callable[[], int],
        clock: Clock,
    ) -> None:
        self.pid = pid
        self.created = created
        self.source = source
        self.epoch = epoch
        self.fmt = FORMAT
        self._meeting_id = meeting_id
        self._callback = callback
        self._privacy_generation = privacy_generation
        self._clock = clock
        self._sample_clock = SampleClock(FORMAT.sample_rate, frames_per_buffer=PACKET_FRAMES)
        self._sequence = 0
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._open_error: BaseException | None = None
        self._failed = False
        self._closed = False
        self._close_lock = threading.Lock()
        self._process: int | None = None
        self._thread = threading.Thread(target=self._run, name="app-loopback", daemon=True)
        # Counters read by diagnostics; written only on the capture thread.
        self.packets = 0
        self.silent_packets = 0
        self.discontinuities = 0
        self.callback_errors = 0
        self.skipped_samples = 0
        self.last_packet_ns: int | None = None

    # -- lifecycle

    def start(self) -> None:
        """Open the target process for liveness checks, then start capture and wait
        until it runs or fails; failures raise and leave nothing open."""
        process = _kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, self.pid)
        if not process:
            raise DeviceUnavailable(APPLICATION_CLOSED)
        if _process_times(process) != self.created or _kernel32.WaitForSingleObject(process, 0) == WAIT_OBJECT_0:
            _kernel32.CloseHandle(process)
            raise DeviceUnavailable(APPLICATION_CLOSED)  # exited, or the id now names another process
        self._process = process
        self._thread.start()
        if not self._ready.wait(OPEN_TIMEOUT_S + 2) or self._open_error is not None:
            error = self._open_error or DeviceUnavailable("process loopback did not start in time")
            self.close()
            raise error

    def target_running(self) -> bool:
        process = self._process
        return bool(process) and _kernel32.WaitForSingleObject(process, 0) != WAIT_OBJECT_0

    def is_active(self) -> bool:
        return not self._closed and not self._failed and self._thread.is_alive() and self.target_running()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(2.0)

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self.stop()
            self._closed = True
            if self._process:
                _kernel32.CloseHandle(self._process)
                self._process = None

    @property
    def max_device_lead_ns(self) -> int:
        return self._sample_clock.max_device_lead_ns

    # -- capture thread

    def _run(self) -> None:
        initialized = _ole32.CoInitializeEx(None, 0) >= 0  # COINIT_MULTITHREADED
        client = capture = 0
        event = None
        try:
            try:
                client = _activate(self.pid)
                fmt = _WAVEFORMATEX(WAVE_FORMAT_IEEE_FLOAT, FORMAT.channels, FORMAT.sample_rate,
                                    FORMAT.sample_rate * FORMAT.channels * 4, FORMAT.channels * 4, 32, 0)
                flags = (AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
                         | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY)
                hr = _method(client, 3, _HRESULT, ctypes.c_int, wintypes.DWORD, ctypes.c_longlong, ctypes.c_longlong,
                             ctypes.POINTER(_WAVEFORMATEX), ctypes.c_void_p)(
                    AUDCLNT_SHAREMODE_SHARED, flags, BUFFER_HNS, 0, ctypes.byref(fmt), None)
                if hr < 0:
                    raise FormatUnsupported(f"process loopback refused 48 kHz float (0x{hr & 0xFFFFFFFF:08x})")
                event = _kernel32.CreateEventW(None, False, False, None)
                if not event or _method(client, 13, _HRESULT, wintypes.HANDLE)(event) < 0:
                    raise DeviceUnavailable("process loopback event could not be set")
                pointer = ctypes.c_void_p()
                if _method(client, 14, _HRESULT, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(
                        ctypes.byref(IID_IAudioCaptureClient), ctypes.byref(pointer)) < 0 or not pointer.value:
                    raise DeviceUnavailable("process loopback capture client unavailable")
                capture = int(pointer.value)
                if _method(client, 10, _HRESULT)() < 0:
                    raise DeviceUnavailable("process loopback could not start")
            except BaseException as exc:  # noqa: BLE001 - reported to start()
                self._open_error = exc if isinstance(exc, LookupError | ValueError | OSError) else \
                    DeviceUnavailable("process loopback failed to open")
                return
            finally:
                self._ready.set()
            self._loop(client, capture, event)
        finally:
            if client:
                try:
                    _method(client, 11, _HRESULT)()
                except OSError:
                    pass
            _release(capture or None)
            _release(client or None)
            if event:
                _kernel32.CloseHandle(event)
            if initialized:
                _ole32.CoUninitialize()

    def _loop(self, client: int, capture: int, event: int) -> None:
        get_buffer = _method(capture, 3, _HRESULT, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32),
                             ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, ctypes.c_void_p)
        release_buffer = _method(capture, 4, _HRESULT, ctypes.c_uint32)
        next_size = _method(capture, 5, _HRESULT, ctypes.POINTER(ctypes.c_uint32))
        data, frames, flags, pending = ctypes.c_void_p(), ctypes.c_uint32(), wintypes.DWORD(), ctypes.c_uint32()
        channels = FORMAT.channels
        while not self._stop.is_set():
            _kernel32.WaitForSingleObject(event, WAIT_MS)
            while not self._stop.is_set():
                if next_size(ctypes.byref(pending)) < 0:
                    self._failed = True
                    return
                if pending.value == 0:
                    break
                if get_buffer(ctypes.byref(data), ctypes.byref(frames), ctypes.byref(flags), None, None) < 0:
                    self._failed = True
                    return
                count = int(frames.value)
                try:
                    if count > 0:
                        if flags.value & AUDCLNT_BUFFERFLAGS_SILENT or not data.value:
                            samples = np.zeros((count, channels), dtype=np.float32)
                            self.silent_packets += 1
                        else:
                            raw = ctypes.string_at(data.value, count * channels * 4)
                            samples = np.frombuffer(raw, dtype=np.float32).reshape(count, channels)
                        discontinuity = bool(flags.value & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY) and self.packets > 0
                        self._emit(samples, count, discontinuity)
                finally:
                    release_buffer(count)

    def _emit(self, samples: np.ndarray, count: int, discontinuity: bool) -> None:
        try:
            observed = self._clock()
            self.last_packet_ns = observed
            self.packets += 1
            placement = self._sample_clock.place(count, observed)
            flags = set()
            if discontinuity:
                self.discontinuities += 1
                flags.add("input_overflow")  # Windows reports a glitch before this packet
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
                sample_rate=FORMAT.sample_rate,
                channels=FORMAT.channels,
                monotonic_ns=placement.monotonic_ns,
                timing_quality=TimingQuality.CALLBACK,
                data=samples,  # copied by AudioFrame
                flags=frozenset(flags),
            )
            self._sequence += 1
            self._callback(frame)
        except Exception:  # noqa: BLE001 - counted, never stops capture
            self.callback_errors += 1


# -------------------------------------------------------------------- backend


class AppLoopbackBackend:
    """CaptureBackend for application sources (system source only)."""

    def __init__(
        self,
        *,
        build: Callable[[], int] = windows_build,
        processes: Callable[[], dict[int, ProcessInfo]] = list_processes,
        windows: Callable[[], list[tuple[int, str]]] = list_windows,
        describe: Callable[[int], str | None] = program_description,
        own_pid: int | None = None,
    ) -> None:
        self._build = build
        self._processes = processes
        self._windows = windows
        self._describe = describe
        self._own_pid = os.getpid() if own_pid is None else own_pid
        self._apps: dict[str, tuple[Application, DeviceDescriptor]] = {}
        self._handles: list[AppLoopbackHandle] = []
        self._lock = threading.Lock()
        self.unsupported_reason: str | None = None

    def supported(self) -> bool:
        try:
            ok = sys.platform == "win32" and self._build() >= MIN_WINDOWS_BUILD
        except OSError:
            ok = False
        self.unsupported_reason = None if ok else UNSUPPORTED
        return ok

    def enumerate(self) -> list[DeviceDescriptor]:
        with self._lock:
            self._apps = {}
            if not self.supported():
                return []
            processes = self._processes()
            for app in group_applications(processes, self._windows(), exclude_pids=self._own_pids(processes)):
                descriptor = DeviceDescriptor(
                    device_id=app.device_id,
                    display_name=clean_name(self._describe(app.pid), app.exe),
                    backend=BACKEND,
                    direction=Direction.RENDER,
                    identity_stability=IdentityStability.UNSTABLE_FINGERPRINT,
                    supports_loopback=True,
                    probed_formats=(FORMAT,),
                )
                self._apps[app.device_id] = (app, descriptor)
            return [d for _app, d in self._apps.values()]

    def _own_pids(self, processes: dict[int, ProcessInfo]) -> set[int]:
        """This app and its own process tree (Tadween never lists itself)."""
        own = {self._own_pid}
        top = top_process(self._own_pid, processes)
        if top is not None:
            own.add(top.pid)
        return own

    def window_titles(self, device_id: str) -> tuple[str, ...]:
        """Window titles from the last listing, for telling instances apart on screen."""
        entry = self._apps.get(device_id)
        return entry[0].titles if entry else ()

    def resolve(self, device_id: str) -> DeviceDescriptor:
        pid, created = parse_device_id(device_id)
        entry = self._apps.get(device_id)
        if entry is not None:
            return entry[1]
        if process_created(pid) != created:
            raise DeviceUnavailable(APPLICATION_CLOSED)
        return DeviceDescriptor(
            device_id=device_id, display_name=clean_name(self._describe(pid), "application"), backend=BACKEND,
            direction=Direction.RENDER, identity_stability=IdentityStability.UNSTABLE_FINGERPRINT,
            supports_loopback=True, probed_formats=(FORMAT,))

    def open(
        self,
        selection: DeviceSelection,
        callback: FrameCallback,
        *,
        meeting_id: UUID,
        epoch: int,
        privacy_generation: Callable[[], int],
        clock: Clock,
    ) -> AppLoopbackHandle:
        if selection.source is not Source.SYSTEM:
            raise ValueError("an application can only be the computer-sound source")
        pid, created = parse_device_id(selection.device_id)
        if not self.supported():
            raise DeviceUnavailable(UNSUPPORTED)
        handle = AppLoopbackHandle(pid=pid, created=created, source=selection.source, meeting_id=meeting_id,
                                   epoch=epoch, callback=callback, privacy_generation=privacy_generation, clock=clock)
        handle.start()
        with self._lock:
            self._handles = [h for h in self._handles if not h._closed] + [handle]
        return handle

    def close(self) -> None:
        with self._lock:
            handles, self._handles = self._handles, []
        for handle in handles:
            handle.close()


class RoutedBackend:
    """One CaptureBackend over the device backend and the application backend.

    The controller receives a single backend for both sources; selections are routed by
    their id, so the microphone and output-device capture stay on the existing backend
    and an application id never reaches it (and vice versa)."""

    def __init__(self, devices: Any, applications: Any) -> None:
        self.devices = devices
        self.applications = applications
        self.application_error: str | None = None

    def _for(self, device_id: str) -> Any:
        return self.applications if is_application_id(device_id) else self.devices

    @property
    def init_error(self) -> str | None:
        return getattr(self.devices, "init_error", None)

    def enumerate(self) -> list[DeviceDescriptor]:
        listed = list(self.devices.enumerate())
        try:
            listed += [d for d in self.applications.enumerate() if is_application_id(d.device_id)]
            self.application_error = getattr(self.applications, "unsupported_reason", None)
        except Exception:  # noqa: BLE001 - devices stay usable if the program list fails
            self.application_error = "application_list_failed"
        return listed

    def window_titles(self, device_id: str) -> tuple[str, ...]:
        titles = getattr(self.applications, "window_titles", None)
        return tuple(titles(device_id)) if callable(titles) else ()

    def resolve(self, device_id: str) -> DeviceDescriptor:
        backend = self._for(device_id)
        resolve = getattr(backend, "resolve", None)
        if callable(resolve):
            return resolve(device_id)
        found = next((d for d in backend.enumerate() if d.device_id == device_id), None)
        if found is None:
            raise DeviceUnavailable("selected source is not present")
        return found

    def open(self, selection: DeviceSelection, callback: FrameCallback, **kwargs: Any) -> Any:
        return self._for(selection.device_id).open(selection, callback, **kwargs)

    def close(self) -> None:
        for backend in (self.applications, self.devices):
            close = getattr(backend, "close", None)
            if callable(close):
                close()
