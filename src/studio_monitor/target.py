"""Target selection, identity validation and rediscovery.

A stored window handle is *never* trusted on its own: before it is used the
window must still exist, belong to a live process whose executable name matches
what was discovered at selection time, and have the same window class.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from .config import TargetIdentity
from .win32.windows import WindowInfo, WindowSystem


@dataclass
class ValidationResult:
    ok: bool
    reason: str = ""
    window: Optional[WindowInfo] = None


def identity_from_window(window: WindowInfo) -> TargetIdentity:
    return TargetIdentity(
        hwnd=window.hwnd,
        pid=window.pid,
        exe_path=window.exe_path,
        exe_name=window.exe_name,
        class_name=window.class_name,
        title=window.title,
    )


def _same_exe(identity: TargetIdentity, window: WindowInfo) -> bool:
    return bool(identity.exe_name) and window.exe_name.lower() == identity.exe_name.lower()


def validate_handle(system: WindowSystem, identity: TargetIdentity) -> ValidationResult:
    """Check that ``identity.hwnd`` still refers to the window we selected."""
    if not identity.is_set:
        return ValidationResult(False, "no target selected")
    if not identity.hwnd or not system.is_window(identity.hwnd):
        return ValidationResult(False, "window handle no longer exists")
    window = system.get_window(identity.hwnd)
    if window is None:
        return ValidationResult(False, "window handle could not be read")
    if not system.process_alive(window.pid):
        return ValidationResult(False, "owning process has exited")
    if not _same_exe(identity, window):
        return ValidationResult(
            False,
            f"handle now belongs to {window.exe_name or 'unknown'!r}, expected {identity.exe_name!r}",
        )
    if window.class_name != identity.class_name:
        return ValidationResult(False, "window class changed; handle was reused")
    return ValidationResult(True, "", window)


def _title_score(identity: TargetIdentity, window: WindowInfo) -> int:
    if not identity.title:
        return 0
    a, b = identity.title.lower(), window.title.lower()
    if a == b:
        return 3
    if a in b or b in a:
        return 2
    first = a.split(" - ")[0].split(" | ")[0].strip()
    return 1 if first and first in b else 0


def rediscover(system: WindowSystem, identity: TargetIdentity) -> Optional[WindowInfo]:
    """Find the Studio main window again after a restart or handle loss.

    Candidates must match the discovered executable name and window class.
    The best candidate is the one with the closest title and the largest area,
    which picks the main window over splash screens and dialogs.
    """
    if not identity.is_set:
        return None
    candidates = []
    for w in system.list_windows():
        if not w.visible or w.tool_window or not _same_exe(identity, w):
            continue
        if w.class_name != identity.class_name:
            continue
        if w.rect.width < 200 or w.rect.height < 150:
            continue
        candidates.append(w)
    if not candidates:
        return None
    # Prefer an exact path match (same install) but tolerate version-folder changes.
    def key(w: WindowInfo):
        same_path = (w.exe_path.lower() == identity.exe_path.lower()) if identity.exe_path else False
        return (_title_score(identity, w), same_path, w.rect.width * w.rect.height)
    candidates.sort(key=key, reverse=True)
    return candidates[0]


def related_windows(system: WindowSystem, main: WindowInfo) -> list[WindowInfo]:
    """Visible dialogs/windows belonging to Studio besides its main window.

    Includes windows from the Studio process tree (Electron helper processes)
    and windows owned, directly or transitively, by the main window.
    """
    tree = system.process_tree(main.pid)
    out = []
    for w in system.list_windows():
        if w.hwnd == main.hwnd or not w.visible or w.cloaked or w.minimized:
            continue
        if w.rect.width < 40 or w.rect.height < 40:
            continue
        in_tree = w.pid in tree
        owned = _owned_by(system, w, main.hwnd)
        if in_tree or owned:
            out.append(w)
    return out


def _owned_by(system: WindowSystem, window: WindowInfo, root_hwnd: int, max_depth: int = 8) -> bool:
    hwnd = window.owner_hwnd
    depth = 0
    while hwnd and depth < max_depth:
        if hwnd == root_hwnd:
            return True
        owner = system.get_window(hwnd)
        if owner is None:
            return False
        hwnd = owner.owner_hwnd
        depth += 1
    return False


def exe_name_of(path: str) -> str:
    return os.path.basename(path) if path else ""
