"""Agent-side HTTP client for the hub (httpx, injectable transport).

The agent credential is ``<device_id>:<secret>``; the secret is issued once at
enrollment and stored in the Windows Credential Manager under
``MonitorScreen/hub-agent/<device_id>`` (never in settings or logs).
"""
from __future__ import annotations

import hashlib
import os
import platform
from dataclasses import dataclass
from typing import Optional

import httpx

from . import __version__
from .credentials import CredentialStore

HUB_CRED_PREFIX = "hub-agent/"


class HubClientError(Exception):
    def __init__(self, message: str, status: int = 0, permanent: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.permanent = permanent


@dataclass
class EnrollResult:
    device_id: str
    secret: str
    workspace_id: str
    heartbeat_interval: int
    unreachable_after: int


def hub_credential_key(device_id: str) -> str:
    return HUB_CRED_PREFIX + device_id


from .config import install_fingerprint  # noqa: E402,F401  (re-exported for callers)


def _redact(text: str) -> str:
    return text.replace("Bearer ", "Bearer <redacted> ") if "Bearer " in text else text


class HubClient:
    def __init__(self, base_url: str, device_id: str = "", secret: str = "", *, verify: bool = True, timeout: float = 10.0,
                 transport: Optional[httpx.BaseTransport] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.device_id = device_id
        self.secret = secret
        self._client = httpx.Client(base_url=self.base_url, verify=verify, timeout=timeout, transport=transport,
                                    headers={"User-Agent": f"studio-monitor-agent/{__version__}"})

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------
    def _auth(self) -> dict:
        if not (self.device_id and self.secret):
            raise HubClientError("agent is not enrolled", permanent=True)
        return {"Authorization": f"Bearer {self.device_id}:{self.secret}"}

    def _request(self, method: str, path: str, **kw) -> dict:
        try:
            r = self._client.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise HubClientError(f"hub unreachable: {_redact(str(exc))[:200]}")
        if r.status_code >= 400:
            detail = ""
            try:
                detail = r.json().get("detail", "")
            except Exception:
                detail = r.text[:200]
            permanent = r.status_code in (400, 401, 403, 404, 413, 415, 422)
            raise HubClientError(f"hub {r.status_code}: {detail}", r.status_code, permanent)
        try:
            return r.json()
        except ValueError:
            raise HubClientError("hub returned a non-JSON response")

    # ------------------------------------------------------------------
    def health(self) -> dict:
        return self._request("GET", "/api/v1/health")

    def enroll(self, code: str, device_id: str, name: str, hostname: str, mode: str, owner_label: str = "",
               expected_account: str = "") -> EnrollResult:
        data = self._request("POST", "/api/v1/enroll", json={
            "code": code, "device_id": device_id, "name": name, "hostname": hostname, "agent_version": __version__,
            "mode": mode, "owner_label": owner_label, "expected_account": expected_account})
        self.device_id, self.secret = data["device_id"], data["secret"]
        return EnrollResult(data["device_id"], data["secret"], data.get("workspace_id", ""),
                            int(data.get("heartbeat_interval", 15)), int(data.get("unreachable_after", 90)))

    def post_events(self, events: list[dict]) -> dict:
        return self._request("POST", "/api/v1/events", json=events, headers=self._auth())

    def heartbeat(self, status: dict) -> dict:
        return self._request("POST", "/api/v1/heartbeat", json={"status": status}, headers=self._auth())

    def upload_evidence(self, event_id: str, path: str, sha256: str = "") -> dict:
        with open(path, "rb") as f:
            data = f.read()
        digest = sha256 or hashlib.sha256(data).hexdigest()
        return self._request("POST", f"/api/v1/events/{event_id}/evidence", headers=self._auth(),
                             files={"file": (os.path.basename(path), data, "image/png")}, data={"sha256": digest})


def load_secret(store: CredentialStore, device_id: str) -> Optional[str]:
    return store.get(hub_credential_key(device_id)) if device_id else None


def save_secret(store: CredentialStore, device_id: str, secret: str) -> None:
    store.set(hub_credential_key(device_id), secret)


def clear_secret(store: CredentialStore, device_id: str) -> None:
    if device_id:
        store.delete(hub_credential_key(device_id))
