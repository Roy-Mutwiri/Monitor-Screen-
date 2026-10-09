"""PC owner name -> notification label ("Roy’s Live").

The owner name is operator-entered text, not a verified TikTok identity. It is
validated here and HTML-escaped wherever it is rendered.
"""
from __future__ import annotations

import socket
import unicodedata

MAX_OWNER_NAME = 60
POSSESSIVE = "’s"   # curly apostrophe + s


class OwnerNameError(ValueError):
    pass


def validate_owner_name(text: str) -> str:
    """Trim; reject line breaks and control characters; cap length. Unicode
    letters and punctuation are fine. Returns the cleaned name ('' allowed)."""
    name = (text or "").strip()
    if any(ch in "\r\n\t\x0b\x0c" for ch in name):
        raise OwnerNameError("The owner name must be a single line.")
    for ch in name:
        if unicodedata.category(ch).startswith("C"):   # Cc, Cf, Co, Cn, Cs
            raise OwnerNameError("The owner name contains control or formatting characters.")
    if len(name) > MAX_OWNER_NAME:
        raise OwnerNameError(f"The owner name is limited to {MAX_OWNER_NAME} characters.")
    return name


def notification_label(owner_name: str, machine_label: str) -> str:
    """'<owner>’s Live', falling back to the machine label when no owner is set."""
    base = (owner_name or "").strip() or (machine_label or "").strip() or socket.gethostname()
    return f"{base}{POSSESSIVE} Live"


def hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:  # pragma: no cover
        return ""
