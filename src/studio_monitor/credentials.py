"""Secure credential storage for bot tokens.

Tokens are kept in the Windows Credential Manager (per-user, encrypted by
Windows, listed under "Windows Credentials" in Control Panel) and referenced
from settings/SQLite only by the bot's UUID. There is no plaintext fallback:
if the store is unavailable the operation fails loudly.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Optional, Protocol

TARGET_PREFIX = "MonitorScreen/telegram-bot/"
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168


class CredentialError(RuntimeError):
    pass


class CredentialStore(Protocol):
    def get(self, bot_id: str) -> Optional[str]: ...
    def set(self, bot_id: str, token: str) -> None: ...
    def delete(self, bot_id: str) -> None: ...
    def reference(self, bot_id: str) -> str: ...


class MemoryCredentialStore:
    """Test double. Never persists anything."""

    def __init__(self) -> None:
        self.tokens: dict[str, str] = {}
        self.fail_set = False
        self.fail_delete = False

    def get(self, bot_id: str) -> Optional[str]:
        return self.tokens.get(bot_id)

    def set(self, bot_id: str, token: str) -> None:
        if self.fail_set:
            raise CredentialError("simulated credential store failure")
        self.tokens[bot_id] = token

    def delete(self, bot_id: str) -> None:
        if self.fail_delete:
            raise CredentialError("simulated credential delete failure")
        self.tokens.pop(bot_id, None)

    def reference(self, bot_id: str) -> str:
        return f"memory:{TARGET_PREFIX}{bot_id}"


if sys.platform == "win32":
    class _FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class _CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", _FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    _advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    _advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(_CREDENTIALW))]
    _advapi.CredReadW.restype = wintypes.BOOL
    _advapi.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
    _advapi.CredWriteW.restype = wintypes.BOOL
    _advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _advapi.CredDeleteW.restype = wintypes.BOOL
    _advapi.CredFree.argtypes = [ctypes.c_void_p]
    _advapi.CredFree.restype = None


class WindowsCredentialStore:
    """Windows Credential Manager (generic credentials, per user)."""

    def __init__(self) -> None:
        if sys.platform != "win32":  # pragma: no cover
            raise CredentialError("Windows Credential Manager is only available on Windows")

    @staticmethod
    def _target(bot_id: str) -> str:
        return TARGET_PREFIX + bot_id

    def reference(self, bot_id: str) -> str:
        return "wincred:" + self._target(bot_id)

    def get(self, bot_id: str) -> Optional[str]:
        pcred = ctypes.POINTER(_CREDENTIALW)()
        if not _advapi.CredReadW(self._target(bot_id), CRED_TYPE_GENERIC, 0, ctypes.byref(pcred)):
            err = ctypes.get_last_error()
            if err == ERROR_NOT_FOUND:
                return None
            raise CredentialError(f"CredRead failed (error {err})")
        try:
            cred = pcred.contents
            size = cred.CredentialBlobSize
            raw = ctypes.string_at(cred.CredentialBlob, size) if size else b""
            return raw.decode("utf-16-le")
        finally:
            _advapi.CredFree(pcred)

    def set(self, bot_id: str, token: str) -> None:
        blob = token.encode("utf-16-le")
        buf = (ctypes.c_byte * len(blob)).from_buffer_copy(blob)
        cred = _CREDENTIALW()
        cred.Type = CRED_TYPE_GENERIC
        cred.TargetName = self._target(bot_id)
        cred.Comment = "Monitor Screen Telegram bot token"
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))
        cred.Persist = CRED_PERSIST_LOCAL_MACHINE
        cred.UserName = "telegram-bot"
        if not _advapi.CredWriteW(ctypes.byref(cred), 0):
            raise CredentialError(f"CredWrite failed (error {ctypes.get_last_error()})")

    def delete(self, bot_id: str) -> None:
        if not _advapi.CredDeleteW(self._target(bot_id), CRED_TYPE_GENERIC, 0):
            err = ctypes.get_last_error()
            if err != ERROR_NOT_FOUND:
                raise CredentialError(f"CredDelete failed (error {err})")


def default_store() -> CredentialStore:
    return WindowsCredentialStore()
