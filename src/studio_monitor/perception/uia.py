"""UI Automation probe. Studio (Electron) exposes no accessible children on
the machine this was developed on (verified: 0 descendants); the probe is
still run first so that a Studio build that enables accessibility is used
automatically. Bounded by a time budget and an element cap."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class UiaElement:
    control_type: str
    name: str
    automation_id: str
    box: tuple[int, int, int, int]    # window-relative x, y, x2, y2
    depth: int


def uia_elements(hwnd: int, window_origin: tuple[int, int], budget_seconds: float = 1.5, max_elements: int = 400,
                 max_depth: int = 14) -> tuple[list[UiaElement], str]:
    """(elements, note). Never raises; a missing library or an empty tree yields ([], note)."""
    try:
        import uiautomation as auto
    except Exception as exc:  # pragma: no cover
        return [], f"uiautomation unavailable: {exc}"
    out: list[UiaElement] = []
    deadline = time.monotonic() + budget_seconds
    ox, oy = window_origin
    try:
        root = auto.ControlFromHandle(hwnd)
    except Exception as exc:
        return [], f"UIA root failed: {exc}"

    def walk(ctrl, depth: int) -> None:
        if time.monotonic() > deadline or len(out) >= max_elements or depth > max_depth:
            return
        try:
            children = ctrl.GetChildren()
        except Exception:
            return
        for ch in children:
            try:
                r = ch.BoundingRectangle
                out.append(UiaElement(ch.ControlTypeName, ch.Name or "", ch.AutomationId or "",
                                      (r.left - ox, r.top - oy, r.right - ox, r.bottom - oy), depth))
            except Exception:
                continue
            walk(ch, depth + 1)
            if len(out) >= max_elements:
                return

    walk(root, 0)
    if not out:
        return [], "no accessible elements exposed by the window"
    named = sum(1 for e in out if e.name)
    return out, f"{len(out)} elements ({named} named)" + (" — truncated" if len(out) >= max_elements else "")
