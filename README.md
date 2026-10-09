# TikTok LIVE Studio Monitor

Watches the **TikTok LIVE Studio** Windows desktop application for popups that need a
human to look at them and sends a Telegram alert with the exact screenshot that triggered
the detection.

Scope is the Studio desktop app only. The TikTok website / browser-based LIVE is **not**
supported and is not part of this project.

Categories detected (rules in `rules/studio_rules.json`):

| Category               | Example wording                                   | Alert                              |
|------------------------|---------------------------------------------------|------------------------------------|
| `verification_puzzle`  | "Verify to continue", "drag the slider", captcha   | **Manual attention required**      |
| `account_suspension`   | "Your account has been suspended"                  | critical                           |
| `live_interruption`    | "Your LIVE has ended", "LIVE was interrupted"      | high                               |
| `restriction_notice`   | "has been restricted", "cannot go LIVE"            | high                               |
| `content_warning`      | "may violate our Community Guidelines"             | medium                             |

The monitor is strictly passive: it never focuses, restores, clicks or dismisses Studio windows.

## How it works

1. **Target selection.** The GUI lists visible top-level windows with the *actual* process
   executable discovered from the running process (`QueryFullProcessImageName`). You pick the
   Studio window; its executable path/name, window class, PID and handle are stored. Nothing
   about the process name is hard-coded. A live preview is shown before monitoring starts.
2. **Regions.** Drag on the preview to add *detection* regions (only these parts are OCR'd) or
   *redaction* regions (blacked out before OCR and before any screenshot leaves the machine).
   Regions are stored as fractions of the window, so they follow moves and resizes.
3. **Capture.** A capture service is bound to the validated Studio HWND as soon as you click
   "Use selected window" (preview starts immediately, independent of Start/Stop). Backends, in
   order of preference:
   - **Windows Graphics Capture** (`wgc`, via the maintained `windows-capture` binding, Windows 10
     1903+): captures the window's own composited surface even when Telegram, Chrome or anything
     else covers it. WGC only emits a frame when the content changes, so the service restarts the
     session as a heartbeat (every `capture.refresh_interval_seconds`, default 15 s) and on size
     change or device loss; a fresh session always yields a frame.
   - **PrintWindow** (`printwindow`): kept only where it produces a non-blank frame. GPU-composited
     Electron windows such as Studio return an empty bitmap while occluded, which was the original
     cause of the RUNNING/DEGRADED flapping.
   - **Desktop crop** (`desktop-crop`): an explicit, clearly labelled last resort used only when the
     window is the foreground window, fully on-screen and the top-level window at five sample
     points of its own rectangle. It is never used silently and never sends another application's
     pixels as Studio evidence.
   Uniform (black/empty) frames are rejected even if the native call succeeded; unchanged static
   content is valid. Frames carry their capture timestamp and backend; the monitor never uses a
   frame older than `capture.max_frame_age_seconds` (30 s) as current evidence. Process identity is
   pid + executable + **process creation time**, so a recycled pid never passes validation.
   Dialogs are captured separately with PrintWindow, limited to windows owned by the main window or
   sibling top-level windows of the same process/executable/class.

   Verified on this machine with self-owned test windows: covered windows are captured correctly,
   moves/resizes are followed. **Minimized windows deliver no frames** (WGC limitation) and are
   reported as "Studio is minimized"; a locked desktop is reported as "Desktop locked". Capture
   resumes automatically on restore/unlock.
4. **OCR + rules.** Windows' built-in OCR (`winocr`, no external binary) reads the text; keyword
   rules classify it. Tesseract and RapidOCR are optional alternatives (`detection.ocr_backend`).
5. **Incidents.** A popup must be seen in `confirm_polls` consecutive polls. The same popup staying
   on screen is one incident (fuzzy text match, digits ignored); it is re-alerted only after the
   cooldown or after it disappears and comes back.
6. **Delivery.** Alerts are written to a SQLite queue *before* any network call and sent by a
   worker with exponential backoff (honouring Telegram `retry_after`). Nothing is lost across
   crashes or outages.
7. **Lifecycle.** Each poll re-validates the stored handle (window exists, process alive, same
   executable name, same window class). If Studio closed or restarted, the window is rediscovered
   and re-validated before monitoring resumes. Status is shown as:
   - `RUNNING`  captures are reliable
   - `DEGRADED` Studio is minimized/hidden/off-screen, capture failed, or only a screen-grab
     fallback was possible while another window is in front
   - `LOST`     window/process gone; waiting for it to reappear

## Broadcast-start alert

On a confirmed NOT_LIVE -> LIVE transition the monitor sends one **"TIKTOK LIVE STUDIO HAS GONE
LIVE"** event (category "Broadcast started / already live", with the exact redacted frame that
supported the LIVE confirmation, "Detected at" time, optional `account_label`). Rules:

- the first confirmed LIVE after monitoring starts is sent as **"IS ALREADY LIVE — monitoring
  started"** and never as a new start;
- UNKNOWN -> LIVE while the same broadcast is already announced sends nothing (observation resumed);
- a confirmed NOT_LIVE re-arms the next start alert; if LIVE is then first seen after an UNKNOWN gap
  longer than the max observation gap, the alert says so and does not assert the start time;
- the last confirmed state and broadcast episode are persisted, so a restart while live reports
  "already live" and never duplicates the start alert;
- a confirmed LIVE ends the offline reminder episode and cancels a still-pending go-live reminder.

Existing enabled bots were migrated to receive this category; new bots get it by default.

## Health alerts (debounced)

Application/session state, capture health, OCR health, broadcast state and Telegram delivery health
are tracked separately and shown immediately in the GUI (friendly text; technical details under
"Diagnostics"). Telegram receives a health message only when a degradation persists for
`health.degrade_after_seconds` (15 s), once per episode even if the cause changes, and a recovery
only after `health.recover_after_seconds` (10 s) of stable health and only if the degradation was
alerted. The episode is persisted to avoid restart spam. Restriction and verification alerts are
independent of this debounce. An unrelated foreground window never degrades window capture.

## Studio activity notifications

Besides popup alerts, the monitor reports what the Studio *application* is doing. These features
work **only while the monitor is running**: nothing is observed, inferred or reported for periods
when the monitor was stopped (the GUI shows "monitor stopped (not observing)").

| Event | When | Message |
|-------|------|---------|
| Studio opened | A new Studio process is detected and a usable screenshot exists (or the screenshot timeout passes) | `TIKTOK LIVE STUDIO OPENED / PC / Time / TikTok LIVE Studio is now running.` |
| Already running | Monitoring starts while Studio is already running | `TIKTOK LIVE STUDIO ALREADY RUNNING ... Studio already running — monitoring started.` |
| Studio closed | The Studio **process** is confirmed gone for `close_debounce_seconds` | `TIKTOK LIVE STUDIO CLOSED ... Image: last available screenshot before closure. Screenshot captured: <ts>` |
| Not-live reminder | Confirmed NOT_LIVE for the threshold | `TIME TO GO LIVE ... confirmed not live for at least 1 hour.` |

A *session* is one run of the Studio main process (keyed by its pid). Window moves, dialogs,
minimizing, hidden/cloaked windows, capture failures, a locked desktop and window recreation by the
same process never produce opened/closed events. A restart (new pid) is reported as closed, then
opened. The closed notification attaches the latest valid redacted frame from the latest-frame
cache (`latest_frame/`, separate from restriction evidence, cleaned by the retention setting) and
labels it with its **original capture time**, never as a post-closure image.

### Broadcast state (LIVE / NOT LIVE / UNKNOWN)

Application running and broadcast live are different states. The broadcast state is derived from
OCR evidence in the configured **live-status regions** (draw them on the preview; without regions
the whole window is used, which is less reliable because chat text can look like controls).
Rules live in `rules/live_state_rules.json`:

- evidence for LIVE: an "End LIVE" control, a LIVE badge **with** an elapsed timer, a viewer count,
  "you're live" status text (a bare "LIVE" word scores nothing);
- evidence for NOT_LIVE: a "Go LIVE" control, preview/offline labels, setup hints;
- transitional screens (loading, connecting, sign-in, updating) and polls where a restriction popup
  is on the main window, capture is unreliable or Studio is not running are **UNKNOWN**;
- contradictory or insufficient evidence is UNKNOWN.

A state is *confirmed* only after `confirm_observations` (default 3) consecutive identical valid
observations with no gap above `max_observation_gap_seconds` (default 30 s) between them.

**The seeded rules are unverified.** They were written from expected Studio wording, not calibrated
on real screenshots. The GUI and the reminder message say "rules unverified" until you set
`"verified": true` in the rules file after calibration (below).

### Reminder semantics (exact)

- Threshold: `offline_threshold_minutes` (default 60) of **confirmed** NOT_LIVE time.
- Scope: while a Studio session is active. Starting Studio in confirmed NOT_LIVE begins an
  *offline episode*; a confirmed LIVE -> NOT_LIVE transition begins a new one.
- Accumulation: time between two consecutive confirmed NOT_LIVE observations counts only if the gap
  is at most `max_observation_gap_seconds` (default 30 s). This is the maximum observation gap that
  can count toward confirmed offline duration. Larger gaps (sleep, lock, UNKNOWN, capture loss,
  monitor downtime) add nothing, and accumulation resumes only after the next fresh NOT_LIVE
  confirmation (`confirm_observations` observations). UNKNOWN never counts.
- One reminder per episode by default. Optional repeats (`repeat_enabled`, off) fire every
  `repeat_interval_minutes` of *additional* confirmed offline time, at most `repeat_max_count` times.
- The reminder is enqueued and marked queued in one SQLite transaction; episode id, accumulated
  seconds, reminders sent and the pending alert id are persisted, so a monitor restart continues the
  episode without counting downtime and without re-sending.
- A confirmed LIVE state or Studio closure ends the episode; a reminder still pending in the outbox
  is cancelled with the reason recorded (`NOT_LIVE_REMINDER_CANCELLED` in history).
- Any alert delivered more than two minutes after it was generated (outage, retries) carries a
  visible "Delayed delivery: sent …, generated …" line.
- A fresh (≤ `fresh_screenshot_max_age_seconds`) redacted frame is attached when available,
  otherwise the reminder is text-only. The monitor never starts or stops a broadcast.

### Calibrating live-state detection on real screenshots

1. Take screenshots of Studio while **not live** (setup screen) and while **live**.
2. Optionally draw live-status regions in the GUI around the Go LIVE / End LIVE control, the
   LIVE badge + timer and the viewer count.
3. Run `studio-monitor calibrate-live not_live.png` and `studio-monitor calibrate-live live.png`
   (or the "Calibrate live state" button). The output shows the OCR text, evidence and result.
4. Adjust phrases/scores in `rules/live_state_rules.json` (or a copy pointed to by
   `activity.live_rules_file`) until both classify correctly, then set `"verified": true`.

### Start at Windows sign-in (optional, off by default)

Settings → Studio activity → "Start Monitor Screen when I sign in to Windows", or
`studio-monitor autostart --enable`. This writes a per-user `HKCU\...\Run` entry that launches the
GUI with `--autostart`, which begins monitoring the saved target. It runs in your interactive
desktop session (required for capture); nothing runs or is observed before you sign in.

## Telegram bots (up to 10)

Alerts go to any number of Telegram bots (maximum 10 saved, disabled ones count). Manage them in the
**Telegram Bots** tab or with `studio-monitor bots ...`.

- A bot = **token** (the sender, from @BotFather; not your Telegram password or a developer API ID/hash)
  + **destination** (chat ID: a user/group/channel id, negative for groups/channels, or a public
  `@username`; optional forum topic id) + **event subscriptions**. Token and chat ID are both required.
- Subscriptions: restriction/content warnings (incl. suspensions and LIVE interruptions), verification
  puzzles, Studio opened, Studio closed, go-live reminders, monitoring health alerts. New bots get all.
- **Tokens are stored in the Windows Credential Manager** under the bot's UUID
  (`MonitorScreen/telegram-bot/<uuid>`). Settings and the SQLite database hold only a credential
  reference and a salted, non-reversible fingerprint (used to reject duplicate tokens). Tokens never
  appear in logs, errors, history, exports or the repository; Telegram URLs are redacted because they
  contain the token. If the credential store is unavailable the bot cannot be saved; there is no
  plaintext fallback.
- **Validate Bot** calls `getMe` only (nothing is sent) and shows the bot's username/id. It proves the
  token, not that the bot may post to the destination. **Send Test** sends an explicit test message with
  a clearly labelled synthetic image to that bot's destination only; the desktop is never captured.
- Editing: leaving the token blank keeps the current one. A new token is validated and must belong to
  the same bot id as before (otherwise add it as a new bot). Destination edits apply to future events;
  already queued deliveries keep their destination snapshot.
- Disabling a bot excludes it from new events and cancels its pending deliveries (reason recorded).
  Removing a bot asks for confirmation, cancels pending deliveries, deletes the credential and keeps
  historical delivery rows (without the token). Messages Telegram already accepted cannot be recalled.

### Delivery model

Every notification is one **event** with one immutable redacted evidence file and one **delivery** per
enabled, subscribed bot (unique per event/bot). Deliveries carry their own destination snapshot,
attempts, next retry, Telegram message id, sanitized error and timestamps. The outbox worker serves
bots in parallel (one in-flight delivery per bot, `delivery_concurrency` bots at once); a failing or
rate-limited bot (Telegram `retry_after` is honoured per bot) never blocks the others.

Delivery is at-least-once: after an ambiguous timeout Telegram may have accepted the message and the
retry can send it again. Exactly-once delivery is not claimed.

History shows event-level summaries ("Delivered to 7 of 10 bots — 2 retrying, 1 blocked"); selecting an
event lists each bot's result, and a failed/dead/cancelled delivery can be retried on its own (bots that
already succeeded are never resent). Evidence is kept while any delivery still needs it; a delivery still
pending after `delivery_max_age_hours` (default 48) is dead-lettered with a visible status so a
permanently blocked bot cannot retain screenshots forever, after which normal retention applies.

### Migration from the single-bot setup

On first start after upgrading, the old token/chat id become **Default Bot** (token moved into the
credential store, removed from settings) and rows of the old outbox become events/deliveries for it:
pending ones exactly once, delivered ones as history only. The migration is versioned, idempotent and
safe to interrupt. `set-telegram` still works and creates/updates "Default Bot".

```powershell
studio-monitor bots list
studio-monitor bots add --name "Alerts" --chat-id -1001234567890 --topic 12      # token prompted (hidden)
studio-monitor bots edit "Alerts" --chat-id 42 --subscribe restrictions,verification
studio-monitor bots edit "Alerts" --rotate-token
studio-monitor bots validate "Alerts"      # getMe only
studio-monitor bots test "Alerts"          # synthetic test notification
studio-monitor bots disable|enable|remove "Alerts"
studio-monitor history --expand
```

## Telegram alert contents

Every alert identifies the source as **TikTok LIVE Studio** and includes: category, detected text,
timestamp, machine label, incident ID, where it was seen (main window or a named separate dialog)
and the triggering screenshot as the photo. Verification puzzles are prefixed with
**Manual attention required**.

## Install (development)

```powershell
py -3.11 -m venv .venv   # requirements include windows-capture + numpy for WGC
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pip install -e .
.venv\Scripts\python -m pytest
```

## Run

```powershell
studio-monitor                       # GUI (default)
studio-monitor list-windows          # enumerate windows; * marks likely Studio windows
studio-monitor select 0x000A0B2C     # store a window as the target
studio-monitor set-telegram --token 123:ABC --chat-id 42
studio-monitor run                   # headless monitoring
studio-monitor run --once            # one poll, print status/detections
studio-monitor calibrate shot.png    # OCR a real Studio screenshot and show which popup rules fire
studio-monitor calibrate-live shot.png  # classify a real Studio screenshot as LIVE / NOT_LIVE / UNKNOWN
studio-monitor history --kind activity  # restriction incidents and Studio activity events
studio-monitor autostart --status       # start-at-sign-in setting
studio-monitor test-alert            # send a test alert through the queue
studio-monitor queue --requeue-failed
```

`STUDIO_MONITOR_TELEGRAM_TOKEN` / `STUDIO_MONITOR_TELEGRAM_CHAT_ID` in the environment are migrated into
"Default Bot" on start (the token is never written to settings); `STUDIO_MONITOR_MACHINE_LABEL` overrides the label.

Config, log, queue database and screenshots live in `%LOCALAPPDATA%\TikTokLiveStudioMonitor`.

## Tuning detection with real screenshots

Take a screenshot of the real Studio popup (Win+Shift+S), then:

```powershell
studio-monitor calibrate "C:\path\to\popup.png"
```

It prints the OCR text, the normalized form, and the matching rules. If nothing matches, copy the
wording into the right category's `any` list in `rules/studio_rules.json` (or a copy pointed to by
`detection.rules_file`). Use `none` to suppress false positives (e.g. the "End LIVE?" confirmation).

## Privacy controls

- `privacy.send_screenshots` — attach screenshots to Telegram (text-only alerts when off)
- redaction regions — blacked out before OCR and before saving/sending
- `privacy.screenshot_retention_days` — local screenshots older than this are deleted
- `privacy.store_detected_text` — keep OCR text in the local incident history or not
- `privacy.log_ocr_text` — OCR text is never logged unless this is on
- `privacy.max_text_in_alert` — bound on detected text in alerts
- bot tokens live in the Windows Credential Manager and are redacted from all errors, logs and URLs

## Build the EXE

```powershell
.venv\Scripts\python packaging\build_exe.py --clean --zip
```

Produces `dist\StudioMonitor\StudioMonitor.exe` (GUI), `dist\StudioMonitor\studio-monitor-cli.exe`
(console) and a zip.

## Layout

```
src/studio_monitor/
  win32/        ctypes bindings, window/process enumeration, capture service (WGC / PrintWindow / desktop crop)
  health.py     health model + debounced health alerts
  broadcast_events.py  broadcast episodes (gone live / already live)
  target.py     identity validation, rediscovery, related dialog discovery
  tracker.py    RUNNING / DEGRADED / LOST lifecycle
  ocr/          windows | tesseract | rapidocr backends
  detection/    rules + detector
  incidents.py  confirmation + de-duplication
  sessions.py   Studio application session (opened / closed)
  framecache.py latest valid redacted frame
  broadcast.py  LIVE / NOT_LIVE / UNKNOWN engine
  reminders.py  offline episodes + not-live reminders
  startup.py    start at Windows sign-in (HKCU Run)
  bots.py       bot registry (max 10), subscriptions, fingerprints
  credentials.py Windows Credential Manager token store
  queue.py      SQLite outbox: events + per-bot deliveries, retry worker
  telegram.py   stdlib Bot API client, token redaction
  bot_tests.py  getMe validation + synthetic test notifications
  alerts.py     alert formatting
  monitor.py    the loop
  gui/app.py    Tkinter UI
  cli.py        command line
rules/studio_rules.json, rules/live_state_rules.json
tests/          pytest suite (fakes for Win32, capture, OCR, Telegram)
packaging/      PyInstaller spec + build script
```
