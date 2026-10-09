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
  end_request.py                  End-LIVE confirmation dialog: spatial OCR match (heading + End now + body/Cancel within
                                  6 lines), 2-frame confirm, episodes persisted in kv_state; outcomes only from trusted evidence
                                  (engine NOT_LIVE -> ended; fresh LIVE after close -> continued; exit/no capture -> unknown)
  perception/                     layout discovery = UIA probe (empty for Studio) + OCR word boxes + visual anchors;
                                  LayoutTracker relocalizes (resize/restart/wholesale change/anchor loss/periodic), worker
                                  thread with latest-frame slot; LayoutStore caches validated profiles. Never hardcode boxes.
  audio/                          AudioSourceResolver -> process loopback (works on this PC via comtypes + IAgileObject
                                  handler) / session meter / input / visual; analyzer never records; no routing changes.
  popups.py / frame_analysis.py   per-frame record: popups (spatial blocks on panels; chat/title/control bar are negative)
                                  BEFORE broadcast scoring; control evidence = label inside the located red button on THIS
                                  frame; dialog text never scores. Timing stamps in payload['timing'] + deliveries columns.
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
.venv\Scripts\python -m pytest -q --ignore=tests/test_gui.py ; .venv\Scripts\python -m pytest -q tests/test_gui.py
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
- End-dialog phrases verified with Windows OCR on the operator's real dialog crop (tests/fixtures/private/, git-ignored);
  the committed fixture is synthetic. Never commit private screenshots. Full-window capture with the dialog: unverified.
- Automatic perception verified on the operator's real frame (private fixture): all core elements located; audio process
  loopback verified to deliver frames from Studio's media process (silence at the time). Not verified: live state on a real
  broadcast, non-English Studio, light theme, OmniParser integration beyond the benchmark. The energy VAD is a fallback only.
- Root cause of 'Has gone LIVE' on the end dialog: phrase-anywhere live scoring (dialog 'End LIVE?', title chip 'Lets Go
  LIVE!', chat 'go LIVE'). Never reintroduce frame-wide control phrases; use LiveRules.classify_frame with exclusions.
- Unknown-popup review (real session 2026-10-09): main UI (sign-in page, empty home panels, docked sources panel) is not
  a dialog. Review needs: button row + >=2-word title + body/second button, centred compact floating panel in the
  app's surface colour (PopupClassifier._on_ui_surface: ad cards in the video preview are not UI), layout located;
  one alert per review cooldown, one-shot (never an incident). Sign-in page = sign_in_screen, LIVE settings sheet /
  go-LIVE setup page (or 3+ blocks spread over >50% of the window) = studio_screen; neither is alerted.
- Post-mortem of a real session: <data_dir>/frame_trace.jsonl has one line per analysed frame (state, scores, evidence,
  control label, popups; never images) and activity_screenshots/BCT-*.png is the redacted frame behind every confirmed
  broadcast transition. Read these before changing any rule after a false transition.
- tests/test_gui.py must run in its own pytest process: in the same process as the rest of the suite a later hub
  dashboard test dies with Windows fatal exception 0x80000003 (Tk + Jinja/starlette interaction; gui+hub alone pass).
- Real Studio while LIVE (2026-10-09): the red 'Go LIVE' button is replaced by the elapsed timer; no 'End LIVE' label.
  LIVE evidence = timer in the control slot (frame_analysis rescans the slot at 2x when no red control is located, score 1)
  + status-bar 'Upload: N kbps' with N>0 (score 1). Never exclude the status bar from broadcast evidence.
  With OCR geometry the Go/End LIVE phrase rules are OFF: control evidence comes only from the located red button, the
  2x control-slot rescan, or text inside the control bar. A promo 'Go Live' elsewhere caused a false end+restart on
  2026-10-09 16:43 before this rule.
- Detectors never evaluate when the broadcast is not LIVE; a 'reconnecting' overlay keeps the LIVE episode open (UNKNOWN,
  not NOT_LIVE) so RECONNECTING can be reported.
