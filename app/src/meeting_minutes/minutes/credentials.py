"""The user's own OpenAI API key, kept in Windows Credential Manager.

`WindowsCredentialStore` calls advapi32 CredWriteW/CredReadW/CredDeleteW through
ctypes (a generic credential, persisted for this Windows user on this computer), so no
new dependency is needed. The key is never written to SQLite, settings, logs, files,
environment variables or exception text, and never appears in a repr. Saving or
removing a key makes no network request. Failures raise `CredentialError` with a fixed
content-free code: unavailable (vault cannot be used; nothing was stored), invalid_key
(empty or malformed input; nothing was stored) or failed.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from datetime import datetime, timezone
from typing import Protocol

TARGET_NAME = "MeetingMinutes/OpenAI"  # the app's fixed generic-credential target
USER_NAME = "OPENAI_API_KEY"
MAX_KEY_CHARS = 1024  # well below CRED_MAX_CREDENTIAL_BLOB_SIZE (5 * 512 bytes)

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2  # this user on this computer; not roamed to other machines
ERROR_NOT_FOUND = 1168


class CredentialError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(f"credential store: {code}")
        self.code = code


def clean_key(key: str) -> str:
    """Strip surrounding whitespace; refuse empty, multi-line, non-ASCII or oversized
    input without echoing it."""
    key = (key or "").strip()
    if not key:
        raise CredentialError("empty")
    if len(key) > MAX_KEY_CHARS or not key.isascii() or any(ch.isspace() or not ch.isprintable() for ch in key):
        raise CredentialError("invalid_key")
    return key


class CredentialStore(Protocol):
    def save(self, key: str) -> None: ...
    def replace(self, key: str) -> None: ...
    def remove(self) -> None: ...
    def exists(self) -> bool: ...
    def get(self) -> str | None: ...
    def saved_at(self) -> datetime | None: ...


class MemoryCredentialStore:
    """In-memory store for tests. `unavailable=True` behaves like a vault that cannot be
    used: every write fails and nothing is kept."""

    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable
        self._key: str | None = None
        self._saved: datetime | None = None
        self._lock = threading.Lock()
        self.writes = 0

    def __repr__(self) -> str:
        return f"MemoryCredentialStore(configured={self._key is not None})"

    def save(self, key: str) -> None:
        key = clean_key(key)
        if self.unavailable:
            raise CredentialError("unavailable")
        with self._lock:
            self._key, self._saved = key, datetime.now(timezone.utc)
            self.writes += 1

    replace = save

    def remove(self) -> None:
        if self.unavailable:
            raise CredentialError("unavailable")
        with self._lock:
            self._key = self._saved = None

    def exists(self) -> bool:
        return self._key is not None

    def get(self) -> str | None:
        return self._key

    def saved_at(self) -> datetime | None:
        return self._saved


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", ctypes.c_uint32),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", ctypes.c_uint32),
        ("AttributeCount", ctypes.c_uint32),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


def _load_advapi():  # pragma: no cover - depends on the platform
    if sys.platform != "win32":
        return None
    try:
        lib = ctypes.WinDLL("advapi32", use_last_error=True)
    except OSError:
        return None
    lib.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), ctypes.c_uint32]
    lib.CredWriteW.restype = ctypes.c_int
    lib.CredReadW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                              ctypes.POINTER(ctypes.POINTER(_CREDENTIALW))]
    lib.CredReadW.restype = ctypes.c_int
    lib.CredDeleteW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
    lib.CredDeleteW.restype = ctypes.c_int
    lib.CredFree.argtypes = [ctypes.c_void_p]
    lib.CredFree.restype = None
    return lib


class WindowsCredentialStore:
    """Generic credential `target` in Windows Credential Manager. `api` and
    `last_error` are injectable so the ctypes boundary can be tested without the vault."""

    def __init__(self, target: str = TARGET_NAME, *, api=None, last_error=None) -> None:
        self.target = target
        self._api = api if api is not None else _load_advapi()
        self._last_error = last_error or ctypes.get_last_error

    def __repr__(self) -> str:
        return f"WindowsCredentialStore(target={self.target!r})"

    @property
    def available(self) -> bool:
        return self._api is not None

    def save(self, key: str) -> None:
        key = clean_key(key)
        if self._api is None:
            raise CredentialError("unavailable")
        data = key.encode("utf-8")
        blob = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        cred = _CREDENTIALW()
        cred.Type = CRED_TYPE_GENERIC
        cred.TargetName = self.target
        cred.CredentialBlobSize = len(data)
        cred.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        cred.Persist = CRED_PERSIST_LOCAL_MACHINE
        cred.UserName = USER_NAME
        try:
            ok = self._api.CredWriteW(ctypes.byref(cred), 0)
        finally:
            ctypes.memset(blob, 0, len(data))
        if not ok:
            raise CredentialError("unavailable")

    replace = save

    def remove(self) -> None:
        if self._api is None:
            raise CredentialError("unavailable")
        if not self._api.CredDeleteW(self.target, CRED_TYPE_GENERIC, 0):
            if self._last_error() != ERROR_NOT_FOUND:
                raise CredentialError("failed")

    def _read(self):
        if self._api is None:
            return None
        pcred = ctypes.POINTER(_CREDENTIALW)()
        if not self._api.CredReadW(self.target, CRED_TYPE_GENERIC, 0, ctypes.byref(pcred)):
            if self._last_error() == ERROR_NOT_FOUND:
                return None
            raise CredentialError("unavailable")
        return pcred

    def exists(self) -> bool:
        try:
            pcred = self._read()
        except CredentialError:
            return False
        if pcred is None:
            return False
        self._api.CredFree(pcred)
        return True

    def get(self) -> str | None:
        pcred = self._read()
        if pcred is None:
            return None
        try:
            cred = pcred.contents
            raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raise CredentialError("failed") from None
        finally:
            self._api.CredFree(pcred)

    def saved_at(self) -> datetime | None:
        try:
            pcred = self._read()
        except CredentialError:
            return None
        if pcred is None:
            return None
        try:
            ft = pcred.contents.LastWritten
            ticks = (ft.dwHighDateTime << 32) | ft.dwLowDateTime  # 100 ns since 1601-01-01 UTC
            return datetime.fromtimestamp((ticks - 116_444_736_000_000_000) / 10_000_000, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        finally:
            self._api.CredFree(pcred)
