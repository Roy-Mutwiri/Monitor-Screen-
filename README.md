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
3. **Capture.** Each poll captures the Studio main window with `PrintWindow(PW_RENDERFULLCONTENT)`
   (works for Electron/Chromium windows even when covered). It also captures every visible window
   that belongs to the Studio process tree or is owned by the main window, so a notice shown as a
   **separate dialog** is captured as its own image, not missed by capturing only the main window.
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

## Telegram alert contents

Every alert identifies the source as **TikTok LIVE Studio** and includes: category, detected text,
timestamp, machine label, incident ID, where it was seen (main window or a named separate dialog)
and the triggering screenshot as the photo. Verification puzzles are prefixed with
**Manual attention required**.

## Install (development)

```powershell
py -3.11 -m venv .venv
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
studio-monitor calibrate shot.png    # OCR a real Studio screenshot and show which rules fire
studio-monitor test-alert            # send a test alert through the queue
studio-monitor queue --requeue-failed
```

Secrets may also come from the environment: `STUDIO_MONITOR_TELEGRAM_TOKEN`,
`STUDIO_MONITOR_TELEGRAM_CHAT_ID`, `STUDIO_MONITOR_MACHINE_LABEL`.

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
- the bot token is masked in all errors and logs

## Build the EXE

```powershell
.venv\Scripts\python packaging\build_exe.py --clean --zip
```

Produces `dist\StudioMonitor\StudioMonitor.exe` (GUI), `dist\StudioMonitor\studio-monitor-cli.exe`
(console) and a zip.

## Layout

```
src/studio_monitor/
  win32/        ctypes bindings, window/process enumeration, PrintWindow capture
  target.py     identity validation, rediscovery, related dialog discovery
  tracker.py    RUNNING / DEGRADED / LOST lifecycle
  ocr/          windows | tesseract | rapidocr backends
  detection/    rules + detector
  incidents.py  confirmation + de-duplication
  queue.py      persistent SQLite delivery queue + retry worker
  telegram.py   stdlib Bot API client
  alerts.py     alert formatting
  monitor.py    the loop
  gui/app.py    Tkinter UI
  cli.py        command line
rules/studio_rules.json
tests/          pytest suite (fakes for Win32, capture, OCR, Telegram)
packaging/      PyInstaller spec + build script
```
