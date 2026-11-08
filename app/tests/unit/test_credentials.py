"""The user's OpenAI key in a credential store (in-memory double and the
Windows Credential Manager ctypes boundary). Synthetic canary keys only."""

from __future__ import annotations

import ctypes
import sys
import uuid

import pytest

from meeting_minutes.minutes.credentials import (
    CRED_PERSIST_LOCAL_MACHINE,
    CRED_TYPE_GENERIC,
    ERROR_NOT_FOUND,
    TARGET_NAME,
    USER_NAME,
    CredentialError,
    MemoryCredentialStore,
    WindowsCredentialStore,
    _CREDENTIALW,
    clean_key,
)

CANARY = "sk-canary-7f3e9b2d4c1a-NOT-A-REAL-KEY"


class FakeAdvapi:
    """Stands in for advapi32: a dict of target -> (blob, persist, user). `fail_write`
    makes CredWriteW fail like an unavailable vault (ERROR_NO_SUCH_LOGON_SESSION)."""

    def __init__(self, vault: dict | None = None, *, fail_write: bool = False, fail_read: bool = False) -> None:
        self.vault = {} if vault is None else vault
        self.fail_write = fail_write
        self.fail_read = fail_read
        self.error = 0
        self._keep: list = []
        self.freed = 0

    def last_error(self) -> int:
        return self.error

    def CredWriteW(self, pcred, flags):  # noqa: N802 - Win32 name
        cred = pcred._obj
        if self.fail_write:
            self.error = 1312
            return 0
        assert cred.Type == CRED_TYPE_GENERIC and flags == 0
        self.vault[cred.TargetName] = (ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize), cred.Persist,
                                       cred.UserName)
        return 1

    def CredReadW(self, target, kind, flags, ppcred):  # noqa: N802
        if self.fail_read:
            self.error = 1312
            return 0
        if target not in self.vault:
            self.error = ERROR_NOT_FOUND
            return 0
        blob = self.vault[target][0]
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        cred = _CREDENTIALW()
        cred.TargetName = target
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        cred.LastWritten.dwHighDateTime = 0x01DB2A00
        self._keep += [buffer, cred]
        ppcred._obj.contents = cred
        return 1

    def CredDeleteW(self, target, kind, flags):  # noqa: N802
        if target not in self.vault:
            self.error = ERROR_NOT_FOUND
            return 0
        del self.vault[target]
        return 1

    def CredFree(self, pcred):  # noqa: N802
        self.freed += 1


def test_memory_store_save_replace_remove() -> None:
    store = MemoryCredentialStore()
    assert not store.exists() and store.get() is None
    store.save(f"  {CANARY}\n")
    assert store.exists() and store.get() == CANARY and store.saved_at() is not None
    store.replace("sk-second-canary")
    assert store.get() == "sk-second-canary"
    store.remove()
    assert not store.exists() and store.get() is None and store.saved_at() is None
    assert CANARY not in repr(store)


@pytest.mark.parametrize("bad", ["", "   ", "sk-abc def", "sk-abc\ndef", "sk-ключ", "x" * 2000])
def test_malformed_keys_are_refused_without_echo(bad: str) -> None:
    with pytest.raises(CredentialError) as info:
        clean_key(bad)
    assert info.value.code in ("empty", "invalid_key")
    assert bad.strip() == "" or bad.strip() not in str(info.value)


def test_unavailable_vault_saves_nothing() -> None:
    store = MemoryCredentialStore(unavailable=True)
    with pytest.raises(CredentialError) as info:
        store.save(CANARY)
    assert info.value.code == "unavailable"
    assert CANARY not in str(info.value) and not store.exists() and store.writes == 0


def test_windows_store_round_trip_and_restart_through_the_ctypes_boundary() -> None:
    vault: dict = {}
    api = FakeAdvapi(vault)
    store = WindowsCredentialStore(api=api, last_error=api.last_error)
    assert not store.exists() and store.get() is None
    store.save(CANARY)
    blob, persist, user = vault[TARGET_NAME]
    assert blob == CANARY.encode() and persist == CRED_PERSIST_LOCAL_MACHINE and user == USER_NAME
    # A new store object (like an app restart) reads the same credential.
    api2 = FakeAdvapi(vault)
    again = WindowsCredentialStore(api=api2, last_error=api2.last_error)
    assert again.exists() and again.get() == CANARY and again.saved_at() is not None
    assert api2.freed >= 3  # every CredReadW result is released with CredFree
    again.replace("sk-replaced-canary")
    assert vault[TARGET_NAME][0] == b"sk-replaced-canary"
    again.remove()
    assert TARGET_NAME not in vault and not again.exists()
    again.remove()  # removing a missing key is not an error
    assert CANARY not in repr(store) and TARGET_NAME in repr(store)


def test_windows_store_unavailable_vault_writes_nothing() -> None:
    vault: dict = {}
    api = FakeAdvapi(vault, fail_write=True)
    store = WindowsCredentialStore(api=api, last_error=api.last_error)
    with pytest.raises(CredentialError) as info:
        store.save(CANARY)
    assert info.value.code == "unavailable" and vault == {}
    assert CANARY not in str(info.value) and CANARY not in repr(info.value)


def test_windows_store_without_advapi_is_unavailable() -> None:
    store = WindowsCredentialStore(api=None)
    store._api = None  # e.g. not Windows
    with pytest.raises(CredentialError):
        store.save(CANARY)
    assert not store.exists() and store.get() is None


def test_unreadable_vault_reports_not_configured_but_get_raises() -> None:
    api = FakeAdvapi({TARGET_NAME: (b"x", 2, USER_NAME)}, fail_read=True)
    store = WindowsCredentialStore(api=api, last_error=api.last_error)
    assert not store.exists()
    with pytest.raises(CredentialError):
        store.get()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager exists only on Windows")
def test_real_credential_manager_with_a_test_only_target() -> None:
    """Writes one generic credential under a unique test-only target and deletes it."""
    target = f"MeetingMinutes/Test/{uuid.uuid4()}"
    store = WindowsCredentialStore(target)
    try:
        store.save(CANARY)
        assert WindowsCredentialStore(target).get() == CANARY
        assert store.saved_at() is not None
    finally:
        store.remove()
    assert not WindowsCredentialStore(target).exists()
