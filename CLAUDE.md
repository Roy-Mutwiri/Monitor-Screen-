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
- All alerts go through `DeliveryQueue` (SQLite outbox) — never call Telegram directly from the loop.
- Tests must not hit the network. Use `FakeWindowSystem`, `FakeCapturer`, `FakeOcr`, `FakeClock`
  (tests/conftest.py) and the `FakeTransport` pattern for Telegram.
- Monotonic time for in-process durations, UTC ISO strings for stored records.

## Layout
```
src/studio_monitor/
  win32/{api,windows,capture}.py  ctypes bindings, enumeration, PrintWindow capture
  target.py / tracker.py          identity validation, rediscovery, RUNNING/DEGRADED/LOST
  detection/{rules,detector}.py   popup keyword rules (rules/studio_rules.json)
  incidents.py                    popup confirmation + de-duplication
  sessions.py                     Studio application session (opened / closed), pid-based
  framecache.py                   latest valid redacted frame (atomic persist, retention)
  broadcast.py                    LIVE / NOT_LIVE / UNKNOWN engine (rules/live_state_rules.json)
  reminders.py                    offline episodes + not-live reminder policy (persisted)
  queue.py                        outbox, history tables, kv state, atomic transactions
  telegram.py / alerts.py         Bot API client, retries, alert text
  monitor.py                      the loop that wires everything per poll
  gui/app.py, cli.py, startup.py  Tkinter UI, CLI, HKCU Run sign-in startup
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
