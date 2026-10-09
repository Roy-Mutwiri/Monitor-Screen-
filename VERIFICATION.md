# Verification status

What has actually been verified, and how. "Synthetic" means pytest with fakes (window system, capture,
OCR, Telegram transport, hub client, psutil, Supermemory client, SMTP sender) and controllable clocks.
Nothing in the synthetic column is production verification.

| Area | Synthetic (pytest) | Real-world | Still required from the operator |
|---|---|---|---|
| Popup detection (restriction / warning / suspension / interruption / verification) | yes | wording **unverified** (`rules/studio_rules.json` seeded) | `studio-monitor calibrate IMAGE` on real Studio screenshots; adjust phrases |
| Live-state engine (LIVE / NOT_LIVE / UNKNOWN) | yes | **unverified** (`rules/live_state_rules.json` `verified: false`) | `calibrate-live` on real LIVE and NOT_LIVE screenshots; set `verified: true` |
| Window-bound capture (WGC) | yes | verified on this PC with self-owned windows and the real Studio window; PrintWindow is blank for Studio | — |
| Telegram delivery (single bot) | yes | **verified by the user** (restriction screenshot delivered) | explicit test notification per bot after adding bots |
| Studio opened / closed / already running | yes | observed on this PC | — |
| Broadcast-start alert, not-live reminders, schedules | yes | **unverified** with a real broadcast | run one real broadcast and check the alert, timing and reminder |
| Account (@username) discovery | yes | menu text **unverified** (no live interaction performed) | header *Detect now* / `account test` during a session |
| Stream-health detectors (reconnecting, missing source, black/frozen preview, presenter, audio meter) | yes | **unverified**; `rules/connection_rules.json` seeded; YuNet smoke-tested only | `detectors text/face/audio IMAGE` on real screenshots; draw Presenter and Audio-meter regions |
| Durable incidents, ack / snooze / maintenance / escalation | yes | — | — |
| Fleet hub (enrollment, ingestion, heartbeats, unreachable, routing, dashboard) | yes (FastAPI TestClient, SQLite, fake Telegram) | **not deployed**; PostgreSQL untested; no real network sync | deploy with `deploy/docker-compose.yml`, enroll one PC, confirm a heartbeat and one routed alert |
| Telegram commands / buttons / single consumer | yes (scripted transport) | **unverified** against the Telegram API | send `/status` from the bot's chat; press a button on a real alert |
| Escalation route, SMTP backup | yes (fakes) | **unverified** | `smtp test`; let one incident go unacknowledged through two reminders |
| Supermemory memory + reports | yes (fake client mirroring SDK 5.0.0) | **unverified**; no key used, no document created | `memory set-key`, enable, finish one broadcast, run `memory search` |
| PC health, watchdog, supervisor, clips, engagement OCR | yes (fake psutil / processes) | doctor ran on this PC; psutil sampling not exercised in a long session | leave `supervise --gui` running for a day; check `[pc health]` in Diagnostics |
| Packaging (PyInstaller) | — | built on this PC after every milestone; bundled CLI smoke-tested (`detectors status`, `hub status`, `smtp status`, `memory status`) | run `StudioMonitor.exe` on the target PCs |

## Acceptance checklist for a real broadcast

1. `studio-monitor doctor` shows no FAIL.
2. Select the Studio window, draw regions (popup, live-status, profile, presenter, audio meter), save.
3. Add a bot, send the explicit test notification, confirm it arrives.
4. Start monitoring; open Studio: expect *STUDIO OPENED*. Go live: expect *HAS GONE LIVE* with screenshot and
   (if detection succeeded) the @username. Check `/status` from the bot's chat.
5. Trigger a harmless popup if possible (e.g. a verification prompt) and confirm the alert, buttons, and the
   *RESOLVED* reply after it disappears; acknowledge via button.
6. Cover the camera for 40 s: expect *PRESENTER NOT VISIBLE*; uncover: expect the *CLEARED* reply.
7. End the broadcast: expect *BROADCAST REPORT*. Close Studio: expect *STUDIO CLOSED* and *SESSION REPORT*.
8. With a hub: dashboard shows the device LIVE during step 4, UNREACHABLE 90 s after you kill the agent, and
   back online afterwards; `/screenshot` from the hub route returns a frame.
