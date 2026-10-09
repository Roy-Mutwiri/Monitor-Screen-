# CLAUDE.md — working notes for this repository

## What this is
A Windows-only monitor for the **TikTok LIVE Studio desktop app** (never the TikTok website).
It OCRs the Studio window, alerts Telegram about restriction/suspension/interruption/verification
popups, and reports Studio activity (opened, closed, not-live reminders). Python 3.11, Tkinter,
ctypes Win32 (no pywin32), SQLite outbox, Windows built-in OCR via `winocr`, PyInstaller.

## Ground rules
- The parent folder `D:\Reproduced Content` contains an unrelated "sanitizer" project. Never touch it.
- The monitor is passive: it must never focus, restore, click or dismiss Studio windows, and must
  never start or stop a broadcast.
- Never hard-code the Studio executable name; it is discovered from the selected window's process.
- Redaction regions are applied **before** OCR and before any image is stored or sent. Every new
  screenshot path (evidence, activity, frame cache) must come from a redacted capture.
- All alerts go through `DeliveryQueue` (SQLite outbox): one event + one delivery per enabled subscribed
  bot (`Monitor.dispatch`). Never call Telegram directly from the loop.
- Bot tokens live only in the credential store (`credentials.py`, Windows Credential Manager) keyed by bot
  UUID. Settings/SQLite hold references and fingerprints. Pass every error string through `telegram.sanitize`.
- Max 10 bots (`bots.MAX_BOTS`); event categories are `bots.EVENT_CATEGORIES` and nothing else.
- Tests must not hit the network. Use `FakeWindowSystem`, `FakeCapturer`, `FakeOcr`, `FakeClock`
  (tests/conftest.py) and the `FakeTransport` pattern for Telegram.
- Monotonic time for in-process durations, UTC ISO strings for stored records.
- Capture is bound to the validated HWND via `CaptureService`; never fall back to desktop cropping silently and
  never treat an unrelated foreground window as a capture problem. WGC emits frames only on change: keep the
  heartbeat (session refresh) separate from pixel-change detection.

## Layout
```
src/studio_monitor/
  win32/{api,windows,capture}.py  ctypes bindings, enumeration, CaptureService (WGC > PrintWindow > verified desktop crop)
  health.py / broadcast_events.py debounced health alerts; broadcast episode dedup
  labels.py                       owner name -> notification label; every headline via alerts.headline()
  contracts/events.py             Event contract (schema v1) shared with the hub
  incident_engine.py              durable incidents: OPEN/RESOLVED, ack, snooze, maintenance, escalation claims,
                                  per-destination root message ids (Monitor.dispatch honours suppression + managed mode)
  schedules.py                    IANA-timezone streaming schedule, DST/overnight, missed start
  hub_client.py / hub_outbox.py / hub_sync.py  agent->hub: httpx client (injectable transport), durable outbox with
                                  deterministic UUID5 event ids, heartbeat/drain loop; Monitor.dispatch mirrors every event
  commands.py / email_backup.py   Telegram commands (/status /screenshot /sessions /ack /snooze /report + buttons),
                                  UpdatePoller = single getUpdates consumer per bot (offset + lease, 409 backoff);
                                  Monitor.execute_remote_command runs ONLY predefined ops (screenshot, status)
  memory.py / session_report.py   Supermemory provider (key only from credential store / hub env, scoped retrieval,
                                  retrieved text = labelled reference), broadcast/session reports from DB facts
  pc_health.py / watchdog.py      psutil sampling -> PC_HEALTH incidents (SustainedCondition); StallDetector + Supervisor
  clips.py / engagement.py        optional GIF ring buffer (redacted frames only); viewer/like counts = observations only
src/hub/                          FastAPI hub: config (env only), db (SQLAlchemy), services (framework-free logic),
                                  delivery (Telegram routes by token_env), app (API + Jinja dashboard); deploy/ has compose
  account.py                      @username discovery: Interactor protocol (Win32Interactor real), perform_lookup,
                                  IdentityStore; Monitor pauses the broadcast engine during the lookup
  target.py / tracker.py          identity validation, rediscovery, RUNNING/DEGRADED/LOST
  detection/{rules,detector}.py   popup keyword rules (rules/studio_rules.json)
  incidents.py                    popup confirmation + de-duplication
  sessions.py                     Studio application session (opened / closed), pid-based
  framecache.py                   latest valid redacted frame (atomic persist, retention)
  broadcast.py                    LIVE / NOT_LIVE / UNKNOWN engine (rules/live_state_rules.json)
  detectors/{text_rules,presenter,audio,suite}.py  stream-health conditions while LIVE (SustainedCondition debounce,
                                  YuNet face boxes only, meter lit-fraction); Monitor._run_detectors opens/resolves incidents
  reminders.py                    offline episodes + not-live reminder policy (persisted)
  bots.py / credentials.py        bot registry + secure token store
  queue.py                        events + per-bot deliveries outbox, history, kv state, transactions
  bot_tests.py                    getMe validation, synthetic test notifications
  telegram.py / alerts.py         Bot API client, retries, alert text
  monitor.py                      the loop that wires everything per poll
  gui/app.py, cli.py, startup.py  ttkbootstrap 2 UI (header/sidebar/pages), CLI, HKCU Run sign-in startup
                                  -> use bootstyle tokens ('primary', 'secondary-outline', '@success' surfaces),
                                     Icon(name) for icons, setup_typography() fonts; tests/test_gui.py drives every button
```

## Commands
```
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m studio_monitor list-windows | run --once | calibrate X | calibrate-live X
.venv\Scripts\python packaging\build_exe.py --clean --zip   -> dist\StudioMonitor\
```

## Verification status
- Popup rules and live-state rules are **seeded from expected wording and unverified** against real
  Studio screenshots. `rules/live_state_rules.json` has `"verified": false`; flip it only after
  `calibrate-live` classifies real LIVE and NOT_LIVE screenshots correctly.
- Everything in tests is synthetic/replay validation, not real-Studio validation.
- Studio exposes NO accessibility tree (UIA/IAccessible children = 0); the profile menu is a separate popup HWND.
  The real menu text was not inspected (interaction on the user's live session was declined), so account
  OCR is unverified; the user verifies with the header 'Detect now' button / `account test`.
- Real Telegram delivery was confirmed by the user (restriction screenshot delivered).
- Broadcast-start detection is verified by synthetic replay only, not with a real broadcast.
- WGC behaviour verified on this machine with self-owned windows (covered, moved/resized, minimized -> no frames).
- Stream-health detectors (Milestone 2): `rules/connection_rules.json` is an unverified seed; YuNet is smoke-tested only
  (loads, no false positive on synthetic frames) — accuracy on real camera framing, the meter reader against Studio's
  real meter and all thresholds are unverified. Presenter conditions are DISABLED without the verified model, never guessed.
- Hub (Milestone 3) is verified only with the FastAPI TestClient + SQLite + FakeTransport and an httpx MockTransport
  agent round trip. Not deployed anywhere; PostgreSQL untested; no real network sync. Agent secrets live in the
  credential store under hub-agent/<device_id>; `cfg.ensure_device_id()` regenerates the id when the install
  fingerprint changes (copied install) and drops enrollment.
- Telegram commands/buttons, escalation route and SMTP backup (Milestone 4) are verified with scripted transports only;
  no real getUpdates session, second chat or SMTP server was exercised. Commands are never arbitrary.
- Supermemory (Milestone 5) is exercised only with a fake client mirroring SDK 5.0.0 shapes; no real key, no real
  documents. Never put an API key in source/tests/logs; `memory.redact_api_key` scrubs sm_… tokens from errors.
- Milestone 6 (PC health/watchdog/clips/engagement/doctor) is verified with fake psutil/processes; `doctor` ran on this
  PC. VERIFICATION.md is the single place that lists real-world vs synthetic status; keep it current.
- Detectors never evaluate when the broadcast is not LIVE; a 'reconnecting' overlay keeps the LIVE episode open (UNKNOWN,
  not NOT_LIVE) so RECONNECTING can be reported.
