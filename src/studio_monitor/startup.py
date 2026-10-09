"""Optional "start when I sign in to Windows" support.

Uses the per-user ``HKCU\\...\\Run`` registry key: it runs in the user's
interactive desktop session (required for the GUI and for window capture),
needs no elevation, and is what the Task Manager "Startup apps" page lists.
Disabled by default. Monitoring only happens while the monitor is running;
nothing is observed, inferred or reported for the time it was not.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional, Protocol

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "MonitorScreen"


class RegistryBackend(Protocol):
    def get(self, name: str) -> Optional[str]: ...
    def set(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...


class WinRegistry:
    """Real HKCU Run key."""

    def _open(self, access):
        import winreg
        return winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, access)

    def get(self, name: str) -> Optional[str]:
        import winreg
        try:
            with self._open(winreg.KEY_READ) as key:
                value, _ = winreg.QueryValueEx(key, name)
                return str(value)
        except OSError:
            return None

    def set(self, name: str, value: str) -> None:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    def delete(self, name: str) -> None:
        import winreg
        try:
            with self._open(winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
        except OSError:
            pass


class DictRegistry:
    """In-memory backend for tests."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, name):
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value

    def delete(self, name):
        self.values.pop(name, None)


def launch_command() -> str:
    """Command that starts the GUI and begins monitoring the saved target."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" gui --autostart'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{exe}" -m studio_monitor gui --autostart'


def is_enabled(registry: Optional[RegistryBackend] = None) -> bool:
    registry = registry or WinRegistry()
    return registry.get(VALUE_NAME) is not None


def enable(registry: Optional[RegistryBackend] = None, command: Optional[str] = None) -> str:
    registry = registry or WinRegistry()
    cmd = command or launch_command()
    registry.set(VALUE_NAME, cmd)
    return cmd


def disable(registry: Optional[RegistryBackend] = None) -> None:
    registry = registry or WinRegistry()
    registry.delete(VALUE_NAME)


def apply_setting(enabled: bool, registry: Optional[RegistryBackend] = None) -> None:
    if enabled:
        enable(registry)
    else:
        disable(registry)
