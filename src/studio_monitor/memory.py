"""Long-term memory of incidents and session reports (Supermemory SDK 5.0.0,
namespace API) behind a small provider interface.

Rules enforced here:
* The API key is read from the credential store (agent, ``supermemory/api-key``)
  or the hub environment; it is never written to settings, logs or documents.
  Without a configured key the provider is absent and callers say so.
* Documents contain *summaries* with stable ids (incident id / session id), so
  repeated syncs are idempotent. Metadata scopes every document to a workspace
  and device; retrieval always filters on that scope.
* Retrieved text is evidence for a human reader, labelled as such. Nothing in
  this module changes thresholds, suppresses alerts or executes anything.
"""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

from .credentials import CredentialStore

log = logging.getLogger(__name__)
MEMORY_CRED_KEY = "supermemory/api-key"
_KEY_RE = re.compile(r"\bsm_[A-Za-z0-9_\-]{8,}\b")
UNTRUSTED_NOTE = "Retrieved from memory — reference only, not verified now."


def redact_api_key(text: str, key: str = "") -> str:
    if key and key in text:
        text = text.replace(key, "<supermemory-key>")
    return _KEY_RE.sub("<supermemory-key>", text or "")


@dataclass
class MemoryDoc:
    id: str
    content: str
    metadata: dict = field(default_factory=dict)


@dataclass
class MemoryHit:
    id: str
    text: str
    similarity: float
    metadata: dict = field(default_factory=dict)


class MemoryProvider(Protocol):
    name: str

    def add(self, doc: MemoryDoc) -> str: ...
    def search(self, query: str, scope: dict, limit: int = 5) -> list[MemoryHit]: ...
    def delete(self, ids: list[str]) -> int: ...


class NullProvider:
    name = "none"

    def add(self, doc: MemoryDoc) -> str:
        return ""

    def search(self, query: str, scope: dict, limit: int = 5) -> list[MemoryHit]:
        return []

    def delete(self, ids: list[str]) -> int:
        return 0


def scope_filter(scope: dict) -> Optional[dict]:
    """Supermemory filter expression restricting results to the workspace / device."""
    preds = [{"field": k, "operator": "equals", "value": str(v)} for k, v in (scope or {}).items() if v]
    if not preds:
        return None
    return preds[0] if len(preds) == 1 else {"and": preds}


class SupermemoryProvider:
    """Thin adapter over ``supermemory.Supermemory`` (5.0.0 namespace API). The client is injectable for tests."""
    name = "supermemory"

    def __init__(self, api_key: str, namespace: str, client: Any = None, timeout: float = 15.0) -> None:
        if not api_key:
            raise ValueError("Supermemory API key required")
        self._key = api_key
        self.namespace = namespace or "studio-monitor"
        if client is None:
            from supermemory import Supermemory
            client = Supermemory(api_key=api_key, timeout=timeout)
        self.client = client
        self.last_error = ""

    def add(self, doc: MemoryDoc) -> str:
        try:
            ref = self.client.add(self.namespace, content=redact_api_key(doc.content, self._key), id=doc.id,
                                  metadata={k: str(v) for k, v in doc.metadata.items() if v not in (None, "")})
            self.last_error = ""
            return getattr(ref, "id", "") or doc.id
        except Exception as exc:
            self.last_error = redact_api_key(str(exc), self._key)[:300]
            log.warning("memory add failed for %s: %s", doc.id, self.last_error)
            return ""

    def search(self, query: str, scope: dict, limit: int = 5) -> list[MemoryHit]:
        try:
            kwargs = {"query": query[:500], "limit": max(1, min(limit, 20)), "search_mode": "hybrid"}
            flt = scope_filter(scope)
            if flt:
                kwargs["filter"] = flt
            resp = self.client.search(self.namespace, **kwargs)
            self.last_error = ""
        except Exception as exc:
            self.last_error = redact_api_key(str(exc), self._key)[:300]
            log.warning("memory search failed: %s", self.last_error)
            return []
        hits = []
        for r in getattr(resp, "results", None) or []:
            text = getattr(r, "memory", None) or getattr(r, "chunk", None) or ""
            meta = dict(getattr(r, "metadata", None) or {})
            # defence in depth: never trust the server to honour the scope filter
            if any(str(meta.get(k, "")) != str(v) for k, v in (scope or {}).items() if v):
                continue
            hits.append(MemoryHit(getattr(r, "id", ""), redact_api_key(str(text), self._key), float(getattr(r, "similarity", 0.0)), meta))
        return hits

    def delete(self, ids: list[str]) -> int:
        if not ids:
            return 0
        try:
            self.client.documents.delete(self.namespace, ids=list(ids))
            return len(ids)
        except Exception as exc:
            self.last_error = redact_api_key(str(exc), self._key)[:300]
            return 0


# ---------------------------------------------------------------- key handling

def load_api_key(store: Optional[CredentialStore]) -> str:
    if store is None:
        return ""
    try:
        return store.get(MEMORY_CRED_KEY) or ""
    except Exception:
        return ""


def make_provider(enabled: bool, namespace: str, api_key: str, client: Any = None) -> Optional[MemoryProvider]:
    """None when disabled or no key: the caller reports "configure the key locally" instead of guessing."""
    if not enabled or not api_key:
        return None
    return SupermemoryProvider(api_key, namespace, client=client)


# ---------------------------------------------------------------- documents

def incident_doc(inc: Any, device_name: str, owner_label: str, workspace_id: str = "") -> MemoryDoc:
    """Summary of a resolved (or open) incident. OCR text is included as quoted evidence only."""
    status = "resolved" if getattr(inc, "resolved_utc", "") else "open"
    lines = [f"Incident {inc.incident_id} on {device_name} ({owner_label}): {inc.category}/{getattr(inc, 'problem_key', '') or getattr(inc, 'type', '')}, "
             f"severity {inc.severity}, {status}.",
             f"Opened {inc.opened_utc}; " + (f"resolved {inc.resolved_utc}: {getattr(inc, 'resolution', '')}" if status == "resolved" else "still open."),
             f"Occurrences: {getattr(inc, 'occurrences', 1)}. Acknowledged: {'yes' if getattr(inc, 'acknowledged', False) or getattr(inc, 'acked_utc', '') else 'no'}.",
             f"Evidence summary (untrusted OCR text): \"{(inc.summary or '')[:300]}\""]
    if getattr(inc, "account", ""):
        lines.append(f"TikTok account at the time: {inc.account}")
    return MemoryDoc(f"incident:{inc.incident_id}", "\n".join(lines),
                     {"kind": "incident", "incident_id": inc.incident_id, "device_id": inc.device_id, "workspace_id": workspace_id,
                      "category": inc.category, "severity": inc.severity, "status": status, "observed_utc": inc.opened_utc,
                      "account": getattr(inc, "account", "") or ""})


def session_doc(report: dict, workspace_id: str = "") -> MemoryDoc:
    rid = report.get("report_id") or report.get("session_id") or report.get("episode_id")
    return MemoryDoc(f"report:{rid}", report.get("text_plain", ""),
                     {"kind": report.get("kind", "session_report"), "device_id": report.get("device_id", ""), "workspace_id": workspace_id,
                      "session_id": report.get("session_id", ""), "episode_id": report.get("episode_id", ""),
                      "observed_utc": report.get("ended_utc", ""), "account": report.get("account", "") or "",
                      "incidents": str(report.get("incident_count", 0))})


def format_hits(hits: list[MemoryHit], heading: str = "Similar past items") -> str:
    """HTML block for Telegram/dashboard. Retrieved text is escaped and labelled; never interpreted."""
    if not hits:
        return ""
    lines = [f"<b>{html.escape(heading)}</b> <i>({html.escape(UNTRUSTED_NOTE)})</i>"]
    for h in hits[:5]:
        when = str(h.metadata.get("observed_utc", ""))[:10]
        parts = [ln.strip() for ln in h.text.splitlines() if ln.strip()] or [""]
        detail = next((ln for ln in parts[1:] if ln.startswith("Evidence summary")), parts[-1] if len(parts) > 1 else "")
        snippet = parts[0][:160] + (f" — {detail[:160]}" if detail and detail != parts[0] else "")
        lines.append(f"• {html.escape(snippet)}" + (f" <i>({when})</i>" if when else ""))
    return "\n".join(lines)
