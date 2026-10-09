# Deploying the hub

The hub is a single FastAPI process in front of PostgreSQL. Nothing here is deployed automatically;
run it on a host you control (a small VPS or an always-on PC behind a reverse proxy with TLS).

```bash
cd deploy
cp .env.example .env            # fill in POSTGRES_PASSWORD, HUB_SECRET_KEY, HUB_ADMIN_API_TOKEN, HUB_BOT_OPS, ...
docker compose up -d --build
curl http://localhost:8080/api/v1/health
```

Local development without Docker: `HUB_DATABASE_URL=sqlite:///./hub.sqlite3 python -m hub serve --port 8080`
(SQLite is for development and tests; use PostgreSQL for a real fleet).

## First-time setup

1. **Admin password** (dashboard): `python -m hub hash-password` → `HUB_ADMIN_PASSWORD_HASH`.
2. **Telegram route** (where managed devices' notifications go). The bot token is an environment
   variable on the hub; the route only stores its *name*:
   ```bash
   curl -X POST http://localhost:8080/api/v1/routes -H "X-Admin-Token: $HUB_ADMIN_API_TOKEN" \
        -H "Content-Type: application/json" \
        -d '{"name":"ops","token_env":"HUB_BOT_OPS","chat_id":"-1001234567890","categories":[],"min_severity":"INFO"}'
   ```
3. **Pairing code** for each PC (single use, expires after 15 minutes by default):
   ```bash
   curl -X POST http://localhost:8080/api/v1/pairing-codes -H "X-Admin-Token: $HUB_ADMIN_API_TOKEN" \
        -H "Content-Type: application/json" -d '{"label":"Roy living room PC"}'
   # or on the compose host: docker compose exec hub python -m hub pairing-code --label "Roy living room PC"
   ```
4. **Enroll the PC** (on the Windows machine, in the monitor):
   `studio-monitor hub enroll --url https://hub.example.org --code ABCD-EFGH-JKLM --mode managed`
   or Settings → *Enroll with pairing code* in the GUI. The agent credential is stored in the Windows
   Credential Manager; `managed` means the hub sends Telegram notifications for that device,
   `standalone` keeps local delivery and only mirrors events to the hub.

## Operations

- Dashboard: `/` devices (LIVE / STUDIO OPEN / ONLINE / UNREACHABLE), `/incidents`, `/events`.
- Reachability: a device is **unreachable** 90 s after its last heartbeat (agents send one every 15 s);
  the hub raises one `DEVICE_UNREACHABLE` incident ("Device unreachable — heartbeat missing") and
  resolves it on the next heartbeat.
- Incidents: `POST /api/v1/incidents/{id}/ack|snooze|resolve` with the admin token.
- Revoke a stolen/copied install: `POST /api/v1/devices/{device_id}/revoke`.
- Evidence (redacted screenshots) is stored under the `hub-evidence` volume, per workspace/device.
- Backups: dump the `hub-db` volume (`pg_dump`) and the evidence volume.

## Security notes

- Put TLS in front (Caddy/nginx/Traefik) and set `HUB_PUBLIC_URL=https://...`; agents verify TLS by default.
- Pairing codes are hashed at rest and single use; agent secrets are hashed with a per-device salt.
- Agents can only write their own telemetry (events with another `device_id` are rejected).
- Telegram tokens and any Supermemory key live only in the hub environment; they are never sent to agents.
