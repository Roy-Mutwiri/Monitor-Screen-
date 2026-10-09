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

## TikTok account discovery

When a broadcast start is confirmed (or monitoring attaches to an already-live Studio), the monitor
reads the broadcasting account’s **@username** once per broadcast episode and includes it in the
start alert:

```
🔴 Roy’s Live — HAS GONE LIVE
TikTok LIVE Studio is broadcasting.
TikTok account: @example_account          (or: unavailable — automatic lookup failed)
Detected at: 2026-10-09 14:03:11 UTC+03:00
```

Flow (bounded to `account.timeout_seconds`, default 10 s): the confirming broadcast screenshot is
saved first and is the image attached to the alert (never the menu). UI Automation is tried first;
on the reference machine Studio’s Chromium accessibility tree is empty, so the monitor then performs a
**guarded physical interaction**: wait for user inactivity (`account.idle_seconds`), revalidate the
window identity (hwnd, pid, process creation time, executable), require Studio to be foreground (it
is brought forward at most once), hit-test the click point, click the profile control, read the popup
menu Studio opens as a separate window (OCR cross-checked over two frames), close only that popup
(Escape while Studio is foreground, otherwise toggle) and restore the previous foreground window if
the user did not switch. One bounded retry is allowed when no menu appeared.

Rules: only an explicit `@handle` counts; a display name alone yields "unavailable". Nothing is
inferred from chat, overlays or arbitrary text. Identity is persisted per broadcast episode (status
NOT_ATTEMPTED / IN_PROGRESS / SUCCEEDED / FAILED, username, display name, source, time, attempts) so
a monitor restart never re-opens the menu for the same broadcast; a new broadcast reads the account
again, and the previous one is shown as "Last detected", never as current. While the menu is open the
broadcast-state engine is paused (no false NOT_LIVE, no offline episode, no second broadcast episode);
restriction detection, capture health and the alert outbox keep running. If the lookup fails or times
out, the start alert is still sent with "unavailable — automatic lookup failed" and the reason.
Subsequent alerts in the same broadcast episode carry the verified handle.

The monitor never clicks Go LIVE / End LIVE, logout, account switching, settings or verification
controls. Automation is limited to the validated Studio window, its profile control and the popup it
opened.

**Calibration:** draw a small *Profile control* region around Studio’s top-right avatar on the
preview (the header is right-aligned and shifts with window width; the default is 190 px from the
right edge, 24 px from the top). The header "Detect now" button and `studio-monitor account test`
run one lookup on demand (they open the menu once); `studio-monitor account status|clear` show or
reset the stored identity. The toggle "Detect account when broadcast starts" (default on) disables
the whole feature.

**Unverified on a real broadcast:** the exact text of Studio’s profile menu (whether it shows the
`@username`) could not be inspected during development, so the OCR reading remains unverified until
you run "Detect now" on your Studio.

## Incidents, acknowledgement, escalation, maintenance, schedules

Every popup detection opens one **durable incident** per device / session / category / problem
episode (`incident_engine.py`, SQLite). Lifecycle (OPEN → RESOLVED) is separate from
acknowledgement: `/ack` or `studio-monitor incidents ack ID` pauses escalation but does not resolve
the fault. Severity defaults: verification, suspension, LIVE interruption and restriction notices are
URGENT (reminder after 5 min, every 5 min, max 3); content warnings are WARNING (30 min, max 2).
Reminders and the resolution notice reply to the incident's root Telegram message, tracked per bot
and destination; if the root is unknown a labelled continuation is sent. Resolution wording follows the
evidence: "no longer visible in Studio (this does not confirm the restriction was lifted)". Frequent
repeats are coalesced into the timeline (`incidents timeline ID`).

**Maintenance / break mode** (`studio-monitor maintenance enter MINUTES [--categories ...]`): selected
expected conditions are recorded but not delivered until the explicit end; restrictions and
verification stay enabled unless deliberately included. Snoozes work per incident, device or category.

**Schedules** (Settings → Device & schedule, or `studio-monitor schedule --enable --tz Africa/Nairobi
--days mon,tue --start 20:00 --end 23:00 --grace 15`): per-device IANA timezone with DST-safe,
overnight-capable windows. Ordinary go-live reminders run only inside scheduled hours; a confirmed
NOT_LIVE past start + grace sends one "scheduled start missed" notice per window. UNKNOWN never counts.

**Device identity**: each installation has a stable `device.device_id` UUID (generated on first save),
a display name, an optional expected TikTok account (compared with the verified observed account),
and a mode: `standalone` (local Telegram delivery) or `managed` (a hub owns delivery; events are
recorded locally and delivered by the hub, so there are no duplicate notifications). Local bot
settings are kept in both modes.

## Popup classification before broadcast transitions (root-cause fix)

**Bug**: opening the *End streaming?* dialog could produce "HAS GONE LIVE" / "ALREADY LIVE". **Root cause**: the live-state
engine scored *phrases anywhere in the frame*: "End LIVE?" inside the dialog counted as the End-LIVE *control*, while
"Lets Go LIVE!" in the title chip and "go LIVE" in the LIVE-chat welcome text counted as the Go-LIVE control. Depending on
what the OCR read on a given frame (a dimmed backdrop, scrolled chat), the balance flipped and a NOT_LIVE/LIVE transition was
manufactured from text that is not the control. Popups were also recognised *after* the broadcast update.

**Fix** (`frame_analysis.py`, `popups.py`, `broadcast.LiveRules.classify_frame`): every frame is analysed as one record
(frame id, capture time, OCR text + boxes). Popups are classified first from spatially grouped blocks on a uniform panel
(title / body / button row); the chat panel, title bar, side panels and the control bar are negative regions. Only then is the
broadcast state scored on the *unobscured* text, and the Go/End LIVE evidence comes from the label read inside the located
red control on **this** frame — never from phrases elsewhere, never from the cached layout. While LIVE, Studio replaces
that button with the elapsed timer (no "End LIVE" label): the control slot is rescanned at 2x and the timer counts as LIVE
evidence together with the status bar's `Upload: N kbps` (N > 0); `Upload: 0 kbps` counts as NOT_LIVE evidence. The end dialog is evidence of
an end *request*; it cannot start a broadcast, reset the episode or combine with another frame. Without OCR geometry the
dialog's own lines are removed from the evidence before classification. A post-LIVE summary never counts as LIVE.
A dialog panel whose full-frame OCR lost a button (white-on-red "End now" is the usual casualty) is rescanned once at 2x
on the panel crop only (same backend, ~15-100 ms); tiles, counters and badges never qualify as unknown dialogs, and the
cards of the post-LIVE summary are never reported as new popups.

**What an unrecognised dialog must look like** (learned from a real session on 2026-10-09, when the sign-in page, the
empty home panels and the docked sources panel each produced a review alert): a block with a button row, a title of at
least two words and a body or a second button, floating centred like a Studio modal (never touching the window edge,
never most of the window), dialog-shaped (at least 15% of the window wide, wider than tall, no repeated rows: a trading
terminal's position list in the preview is a table, not a dialog), drawn in the app's own surface colour (a white ad card
inside the dark preview is video, not UI),
seen only after the layout has been located. The sign-in page (QR / Google / email-password / confirm on mobile) is one
`sign_in_screen` observation and is not alerted; the LIVE settings sheet / go-LIVE setup page (LIVE info, Moderators,
About me, Video settings, camera-source and speed-test notes) and any three-plus unrelated blocks spread over the window
are one `studio_screen` observation, also not alerted. Review alerts carry every unrecognised block of the frame, are sent at
most once per `detection.review_cooldown_seconds` (default 300 s; later distinct blocks are counted into the next
alert) and are one-shot notices: no incident, no reminders, nothing to `/ack`.

**Typed popup result**: `popup_type` (end_stream_confirmation, live_restriction, live_access_suspension, account_suspension,
verification_challenge, reconnecting, missing_source, post_live_summary, informational, unknown), title, body,
button_labels, bounding_box, observed_at, frame_id, confidence, classification_reason, evidence. LIVE-access suspensions are
never upgraded to account suspensions. Unknown dialogs produce `💬 NEW STUDIO POPUP — NEEDS REVIEW` with the readable
title/body/buttons and the screenshot (bot category *popups*), never a guessed restriction.

**End dialog caption**: `🟠 <owner>’s Live — END-LIVE CONFIRMATION OPENED`, TikTok account, Title / Message / Buttons as
read, "The broadcast has not yet been confirmed ended.", "Observed: <time>", exact triggering screenshot.

**Latency**: every alert payload carries `timing` (frame id, capture time, analysis ms, persisted time); deliveries record
request start / response (monotonic). `studio-monitor latency` and Diagnostics `[latency]` show median / p95 of
detection→persist, queue delay and Telegram API time plus queue depth. Credible dialogs with a known category alert on the
first poll (`detection.immediate_strong_evidence`); weaker evidence still needs `confirm_polls`. Default poll interval is
now 1 s. Urgent kinds are dequeued first; a rate-limited bot only blocks itself (`retry_after` honoured per bot). Delayed
deliveries carry "Observed at …; delivery delayed by …; not the current status".

## Automatic perception (no regions to draw)

Select the Studio window, press **Start**, and the monitor discovers the layout itself (`perception/`):

1. **Accessibility first**: the window's UI Automation tree is probed (bounded). The Studio build on the development PC
   exposes *no* accessible children (verified: 0 descendants), so this path is empty there; a build that enables
   accessibility is used automatically and takes precedence (named Go LIVE / chat / profile controls).
2. **OCR with geometry**: Windows OCR returns word boxes; anchor words (Studio view, Add source, Tools, LIVE chat,
   LIVE performance, Go LIVE / End LIVE, CPU/Memory/Upload/FPS, LIVE Center) establish the top bar, the side panels
   (also when rearranged or closed), the control bar and the status row.
3. **Visual anchors** inside those bands: the red Go/End LIVE button blob, the program preview as the largest
   non-canvas rectangle between the panels (the portrait video column on the real frame, not thumbnails, not chat
   avatars), slider tracks and the green level segment in the control row (mixer / audio meter), a circular control
   right of "LIVE Center" in the top bar (profile), the LIVE badge/timer text, and spatially grouped text blocks with
   button words floating over the preview band (dialogs and banners).

Every element is a structured observation: type, bounding box, confidence, evidence source (`uia`, `ocr`, `visual`,
`ocr+visual`, `cache`, `omniparser`), frame timestamp, layout version and validity. Nothing is a hard-coded
percentage: cached relative coordinates (`layout_profiles.json`, scoped by Studio version, language, window-size
bucket, DPI and the panel column split) are written only after an evidence-based discovery and are revalidated
against the current frame before use; a cached profile contradicted by fresh evidence is ignored.

**Relocalization**: a cheap validation runs every 5 s (red button still at its box, preview still non-uniform, left
anchors still on the left) and discovery re-runs on window resize, Studio restart, wholesale frame change (scene or
panel change), anchor contradiction, the 60 s periodic refresh, or *Re-detect layout*. Discovery runs on its own
worker thread with a latest-frame slot (no backlog); measured on the real frame: OCR 67 ms + discovery ~55 ms (CPU).
Stale elements are invalidated; while nothing is located the UI says *Studio layout: locating…*, *Presenter region
unavailable* or *Locating audio meter* instead of reporting an absent face or silence.

**Presenter**: faces are searched only inside the verified program preview; several comparable faces or a tiny face
are reported as *unclear* (no silent choice); FACE_ABSENT, FACE_MOTION_LOW, PREVIEW_FROZEN and UNKNOWN stay separate
with the existing thresholds and hysteresis. Movement is never taken as proof of a real human.

**Audio** (`audio/`): `AudioSourceResolver` enumerates Studio's process tree, its Windows audio sessions and the
endpoints, reads device names from Studio's own UI text, checks OS support, and binds — in order — Windows **process
loopback** of Studio's rendered audio (verified on this PC: activation succeeds and frames flow from Studio's media
process; what Studio plays locally, so microphone audio that Studio uploads but does not play may be missing — never
described as complete broadcast audio), the Studio **session level meter** (level only), an **input device** named
by Studio's UI when exactly one endpoint matches (otherwise one simple choice is offered), or the **visual meter**.
Analysis runs in ≤3 s buffers that never touch disk: RMS/peak, sustained silence (music without speech is not
silence; "no speech" never raises an alert), clipping, speech activity (webrtcvad, energy fallback), recovery.
Optional local transcription (faster-whisper, operator-installed) is VAD-gated, chunked with overlap de-duplication
and hallucination suppression, and never delays audio-health alerts. Routing, monitoring and mute states are never
changed.

**Dialogs**: the end-stream confirmation and other modals are now found by spatial grouping of OCR boxes (heading,
body and button row as one block floating over the preview) in addition to the line-adjacency rule. No discovered
control is ever clicked; the profile-menu lookup remains the only automatic interaction and is refused when the
profile control is not located with enough confidence.

**Privacy**: manual masks are kept and always applied first; the detected LIVE chat panel is masked automatically
(viewer names) unless disabled; automatic discovery never removes a mask; overlays and CLI dumps use redacted frames;
no screen or audio content is sent to any cloud perception service (the optional OmniParser runs locally).

**UI**: Monitor page shows *Studio layout / Presenter / Audio / Detectors* status, a *Show detected areas* overlay
and *Re-detect layout*; manual region tools live under *Advanced: override automatic detection* and existing manual
regions keep precedence until cleared. CLI: `perception status|discover [--overlay PATH]|clear-cache`,
`audio status|probe`.

**Evaluation**: see `docs/perception/SOURCE_MANIFEST.md` (references, licences, tested combinations) and
`docs/perception/OMNIPARSER_EVALUATION.md` (licence review and the GPU/CPU benchmark on this PC). No model was trained.

## End-LIVE confirmation alert ("End streaming?")

When the operator clicks *End LIVE*, Studio shows a confirmation dialog (heading **End streaming?**, body
**End LIVE? Share your LIVE for more viewers.**, buttons **End now** / **Cancel**). `end_request.py` detects that
combination from spatially related OCR evidence: the heading line, the *End now* button and the body/*Cancel* must
appear within six consecutive OCR lines of the Studio frame (or a 220-character window when the OCR backend gives no
line structure). *End LIVE* or *End* alone never matches. Two consecutive fresh frames within 12 s confirm it; the
exact triggering frame (already redacted) is kept as evidence.

The alert (`🟠 <owner>’s Live — LIVE IS BEING ENDED`, verified `@username` or "unavailable", "Ending has not yet been
confirmed", detection time, screenshot) goes through the normal delivery owner (local bots in standalone mode, the
hub in managed mode) under the broadcast category, as event type `BROADCAST_END_REQUESTED` and an INFO incident.

The dialog is tracked as its own **end-request episode** and never changes the broadcast state, ends the session,
starts the offline timer or produces the report. While it is open, stream-health detectors and the username lookup
are held (the dialog obscures their regions). Outcomes, decided only by evidence the monitor already trusts:

- **ended** — the live-state engine confirms NOT_LIVE: `⚫ LIVE HAS ENDED` replies in the same incident thread, the
  broadcast report is produced exactly once by the existing path, and the confirmed end time is recorded;
- **continued** — the dialog is gone on fresh frames (two misses) and fresh evidence confirms LIVE:
  `🟢 END CONFIRMATION CLOSED — LIVE CONTINUES` (it never claims *Cancel* was clicked);
- **unknown** — Studio exits, or no valid capture arrives within 180 s: recorded in history and the incident
  timeline with the reason; no end claim is made and the existing Studio-exit/health notices carry the evidence.

Invalid capture never counts as the dialog disappearing. One alert per open dialog; closing and reopening starts a
new episode. The episode is persisted, so after a monitor restart an open dialog is not re-announced and a pending
outcome is reconciled with fresh frames (bounded; unknown if none arrive). Outcomes appear in session/broadcast
reports and in the resolved incident that is synced to memory; detection timestamps are preserved when events reach
the hub late. Calibration: the phrases were verified with Windows OCR on the operator's real dialog crop (kept
locally, not committed); the committed fixture is synthetic. A full Studio-window capture with the dialog open has
not been exercised yet.

## PC health, watchdog, clips and engagement

- **PC health** (`pc_health.py`, psutil): CPU, memory, free disk on the data drive, battery (only when
  discharging), the Studio process load, and whole-PC upload throughput (checked only while LIVE and only when a
  minimum is configured). A threshold must hold for 2 minutes (default) to open one health-category incident per
  condition; recovery after 1 minute resolves it with a threaded notice. Samples appear in Diagnostics, `/status`
  and the hub heartbeat. These are measurements of the PC, not of Studio's stream.
- **Watchdog**: an in-process stall detector flags a monitor loop that has not completed a poll for 2 minutes
  (logged, shown in the heartbeat as `stalled`); `studio-monitor supervise [--gui]` runs the monitor under a
  restarting supervisor (exponential backoff, at most 10 restarts per hour, stops on a clean exit). The hub's
  unreachable detection covers the case where the whole PC is gone.
- **Clips** (`clips.py`, off by default): a ring buffer of the last 10 s of already-redacted frames (1 fps) is
  written as a GIF when a popup alert is raised and sent as a reply to the alert (`sendAnimation`); failures never
  affect the alert. No audio is recorded.
- **Engagement** (`engagement.py`): viewer and like counts parsed from Studio's own on-screen counters in the
  live-status OCR text (`1,204 viewers`, `12.5K`), tracked per broadcast episode (latest / peak / average) for
  `/status` and the broadcast report. Observations only; they never trigger alerts.
- **Hardening**: `studio-monitor doctor` checks the credential store, OCR backends, Windows Graphics Capture, the
  face model checksum, disk space, target, bots, rule verification state, sign-in startup and hub enrollment.
  Log redaction now also covers Supermemory keys and agent credentials; settings are written atomically; evidence
  uploads are type-checked (PNG/JPEG/GIF) and size-limited.

## Deployment guide

**One PC (standalone)**: unzip `StudioMonitor-<version>-win64.zip`, run `StudioMonitor.exe`, select the Studio
window, draw regions, add a bot and send the test notification, set *Whose PC?*, enable *Start at sign-in* (or run
`studio-monitor-cli.exe supervise --gui` from a shortcut for crash restarts). Run `studio-monitor-cli.exe doctor`.

**Several PCs with a hub**: follow `deploy/README.md` (Docker Compose: PostgreSQL + hub behind TLS), create a
Telegram route and a pairing code per PC, then on each PC `studio-monitor-cli.exe hub enroll --url https://hub…
--code CODE --mode managed` (hub sends notifications) or `--mode standalone` (PC sends, hub mirrors). Enable
`commands_enabled` on the route for `/status`, `/screenshot DEVICE`, `/ack`, `/snooze`, `/report` from Telegram.
Optional: `SUPERMEMORY_API_KEY` on the hub for long-term memory; `smtp` settings per PC for the e-mail backup.

See `VERIFICATION.md` for what has and has not been verified against real services, and the acceptance checklist.

## Reports and long-term memory (Supermemory)

**Reports** (`session_report.py`): when a broadcast ends (confirmed NOT_LIVE) a *broadcast report* goes to bots
subscribed to the broadcast category, and when Studio closes a *session report* goes with the Studio-closed
category (suppressed when closed notifications are off). Content is computed from the database only: duration,
incidents by category with open/resolved counts and total time, reminders, stream-health episodes, the verified
account, and a note when live-state rules are unverified. `/report` returns the live equivalent on demand.
Toggle: Settings → Memory → *Broadcast / session reports*.

**Memory** (`memory.py`, Supermemory SDK 5.0.0 namespace API): resolved incidents and reports are stored as short
summaries with stable ids (`incident:<id>`, `report:<id>`, so re-syncs are idempotent) and metadata that scopes them to
workspace and device. Retrieval always filters on that scope (and re-checks it client-side) and is shown under
“Similar past incidents / sessions — retrieved from memory, reference only, not verified now” in `/report` and the hub
API. Retrieved text is escaped and never interpreted: it cannot change thresholds, suppress alerts or run anything.

- **Standalone PC**: `studio-monitor memory set-key` stores the API key in the Windows Credential Manager
  (`MonitorScreen/supermemory/api-key`); it is never written to settings, logs or documents (keys are redacted from
  error messages). Enable in Settings → Memory. Without a stored key the feature reports “key missing” instead of
  guessing.
- **Hub**: `SUPERMEMORY_API_KEY` in the hub environment only; one namespace per workspace; the background loop syncs
  resolved incidents and reports every minute (`POST /api/v1/admin/memory-sync` runs it now); `GET
  /api/v1/memory/search?q=…&device_id=…` and the hub `/report` command retrieve. Agents never receive the hub key, and
  managed devices do not run their own memory sync.

**Not verified against the real service**: all Supermemory calls are exercised with a fake client that mirrors the
SDK 5.0.0 request/response shapes; no real key was used and no real document was created.

## Telegram commands, buttons, escalation and e-mail backup

**Commands** (`commands.py`): `/status`, `/screenshot`, `/sessions`, `/ack INCIDENT`, `/snooze INCIDENT MINUTES`,
`/report`, `/help`. Incident alerts carry inline **Acknowledge / Snooze 30 min / Screenshot now** buttons that do the
same. Only the bot's configured chat may issue commands (others are ignored and audited), there is a per-chat rate
limit, and every command is a predefined operation: nothing typed in Telegram is ever executed. Acknowledging pauses
reminders but never resolves the fault; the resolution still comes from Studio no longer showing the problem.

**Single consumer per bot**: in standalone mode the monitor long-polls `getUpdates` for one bot (Settings → *Telegram
commands*, default: the first enabled bot). The offset is persisted after processing (Telegram replays are
deduplicated by update id), an in-process lease stops two pollers in one installation, and Telegram's `409 Conflict`
(someone else polling the same bot) pauses polling for 60 s and is reported. In **managed** mode the hub is the only
consumer: routes with `commands_enabled` answer `/status` (fleet), `/screenshot [DEVICE]`, `/sessions`, `/ack`,
`/snooze`, `/report`. `/screenshot` queues the predefined `screenshot` operation for that device; the agent executes it
on its next heartbeat and mirrors a `SCREENSHOT` event with redacted evidence, which the hub routes back to Telegram.
Unknown operations are recorded and ignored.

**Escalation route** (Settings → *Telegram commands & escalation*): once an incident stayed unacknowledged through N
reminders, an extra message goes to a second chat (same or another bot). **E-mail backup** (`email_backup.py`, SMTP
with STARTTLS or SMTPS, password in the Credential Manager via `studio-monitor smtp set-password`): when a Telegram
delivery of an URGENT event has definitively failed, a plain-text e-mail with the alert text is sent once per failed
delivery. `studio-monitor smtp test` sends an explicit test e-mail. Both are verified with fakes only; no real SMTP
server or second Telegram chat has been exercised.

## Fleet hub (multi-PC)

`src/hub` is a central FastAPI + SQLAlchemy service (PostgreSQL via `deploy/docker-compose.yml`, SQLite for
development/tests) that many monitor installations report to. See `deploy/README.md` for setup.

- **Enrollment**: an operator creates a single-use, expiring pairing code (`POST /api/v1/pairing-codes` or
  `python -m hub pairing-code`); the PC runs `studio-monitor hub enroll --url URL --code CODE --mode managed|standalone`
  (or Settings → *Enroll with pairing code*). The hub issues a per-device secret (stored hashed with a per-device salt
  on the hub, and in the Windows Credential Manager under `MonitorScreen/hub-agent/<device_id>` on the PC; never in
  settings or logs). Re-enrolling rotates the secret; revoking a device invalidates it.
- **Device identity**: `device.device_id` is a UUID bound to an install fingerprint (machine GUID + Windows user + data
  dir). A copied installation gets a fresh id and its enrollment is dropped, so it must enroll as a new device.
- **Events**: every dispatched notification is also written to a durable agent outbox (`hub_outbox` table) as a
  schema-v1 contract event with a deterministic UUID (so retries deduplicate) and uploaded in batches with exponential
  backoff; the hub accepts, deduplicates by `event_id`, rejects events for other devices, mirrors incidents
  (open / occurrence / resolve) and stores redacted evidence after a SHA-256 check. Offline periods lose nothing.
- **Heartbeats**: every 15 s with live/app/capture state, account and outbox depth. No heartbeat for 90 s →
  `DEVICE_UNREACHABLE` ("Device unreachable — heartbeat missing for N s"), one incident per outage, resolved by the
  next heartbeat. Hub-originated events are always routed because the agent cannot report its own absence.
- **Modes**: `standalone` keeps local Telegram delivery and only mirrors to the hub; `managed` withholds local
  delivery and the hub routes notifications via **routes** (`POST /api/v1/routes`: workspace, categories, minimum
  severity, chat id, and the *name* of the environment variable holding the bot token on the hub). Snoozed incidents
  are not routed; resolutions always are. Tokens never leave the hub.
- **Dashboard**: `/` devices with status (LIVE / STUDIO OPEN / ONLINE / UNREACHABLE / NEVER SEEN), expected vs
  observed account with mismatch flag, `/incidents`, `/events`, `/devices/{id}`; optional password login
  (`HUB_ADMIN_PASSWORD_HASH`) and an admin API token for automation. Remote commands returned with heartbeats are
  recorded by the agent but not executed in this version.

**Not verified in production**: everything above is validated with the FastAPI test client, SQLite and fake
Telegram transports. No hub has been deployed (no deployment target is configured), PostgreSQL has not been
exercised, and no agent has synced across a real network.

## Stream-health detectors (while LIVE)

`detectors/` evaluates the fresh Studio frame only while the broadcast is confirmed LIVE (or shows a
transitional/unreadable screen inside an open LIVE episode). Every condition needs sustained evidence,
pauses on invalid frames (UNKNOWN), restarts after an evidence gap longer than 10 s, and recovers only
after the problem has been absent for a while. One incident per condition episode; recovery replies to
the original message; incidents are closed quietly when the broadcast ends. Nothing here changes the
LIVE / NOT_LIVE state and nothing clicks Studio.

| Condition | Evidence | Default |
|---|---|---|
| `RECONNECTING` | connection wording from `rules/connection_rules.json` (unverified seed) in the OCR text | 10 s sustain, 20 s recover |
| `SOURCE_MISSING` | explicit "camera unavailable / source not found / file not found" wording | 10 s |
| `BLACK_PREVIEW` | presenter region mean luminance below 12 | 20 s |
| `FACE_ABSENT` | no face in the **Presenter** region (profile "presenter expected" only) | 30 s |
| `FACE_MOTION_LOW` | face visible, in-face motion below threshold (background motion ignored) | 60 s |
| `PREVIEW_FROZEN` | presenter region pixel-identical while the rest of the frame keeps changing | 20 s |
| `AUDIO_SILENCE` | Studio's on-screen **Audio meter** region shows no lit segments; unreadable meter = UNKNOWN | 30 s |

Face detection uses OpenCV's YuNet (`models/face_detection_yunet_2023mar.onnx`, Apache-2.0, SHA-256
pinned) on the CPU and returns boxes and landmarks only; no embeddings, no identity, nothing leaves the
PC. If the model is missing or fails verification the presenter conditions are reported DISABLED rather
than approximated. A large whole-frame change (scene switch) pauses presenter evaluation for 10 s and
restarts its timers; the profile-menu account lookup and maintenance mode suppress it too. Regions:
draw a **Presenter** box over the camera preview and an **Audio meter** box over Studio's level meter.
Settings → "Stream health (while LIVE)" holds the thresholds and the presenter/audio profiles; bots
receive these alerts through the new `stream_health` category (existing enabled bots are subscribed
by the version-5 settings migration). CLI: `studio-monitor detectors status | text IMAGE | face IMAGE |
audio IMAGE` for calibration against real screenshots.

**Not verified on a real broadcast**: the connection/source wording, the YuNet accuracy on this
machine's camera framing, the meter reader against Studio's real meter and all thresholds are
synthetic-replay validated only.

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

## Interface

The desktop app is built with Tkinter + [ttkbootstrap 2](https://github.com/israel-dryer/ttkbootstrap)
and follows the Windows type ramp (Segoe UI Variable Text/Display: caption 9 pt, body 10 pt,
subtitle 12 pt semibold, title 16 pt; Cascadia Mono for logs and diagnostics). Icons are Bootstrap
Icons rendered by ttkbootstrap, not emoji. Dark and Light themes (Settings → Appearance).

- **Header:** brand, "Whose PC?" owner field with Save, status pills (monitoring, capture health,
  broadcast state), Start / Stop.
- **Monitor:** Studio window picker, live preview with region tools (popup detection, redaction,
  live-status), stat tiles (capture, Studio, broadcast, go-live reminder, Telegram delivery), activity log.
- **Telegram Bots:** bot table with Add / Edit / Remove / Enable-Disable / Validate / Send Test.
- **History:** events with delivery summary and owner label, per-bot delivery details, single-delivery
  retry, open screenshot.
- **Settings:** all options as grouped forms with Save / Revert and validation.
- **Diagnostics:** technical capture/identity/outbox details, copy to clipboard, open data folder,
  calibration of popup and live-state rules on real screenshots.

Every button is exercised by `tests/test_gui.py` against the real (withdrawn) window with dialogs and
network replaced by test doubles.

## Whose PC? (owner label)

The "Whose PC?" field at the top of the Monitor tab (or `studio-monitor owner NAME`) sets an operator
label used in **every** notification headline, e.g. with owner "Roy":

```
🔴 Roy’s Live — HAS GONE LIVE          ⚠️ Roy’s Live — RESTRICTION DETECTED
🧩 Roy’s Live — VERIFICATION REQUIRED   ⏰ Roy’s Live — GO-LIVE REMINDER
🟢 Roy’s Live — STUDIO OPENED           ⚫ Roy’s Live — STUDIO CLOSED
⚠️ Roy’s Live — MONITOR DEGRADED        🧪 Roy’s Live — TEST NOTIFICATION
```

Rules: trimmed, single line, no control characters, at most 60 characters, Unicode and punctuation
allowed. Blank falls back to the machine label (`<machine label>’s Live`). The hostname and machine
label stay visible under Diagnostics. The label is operator-entered text, not a verified TikTok
identity, and is HTML-escaped. Each event stores the label in force when it was created; changing the
name affects future notifications only, and already queued ones keep their original text.

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
  detectors/    stream-health conditions: text rules, presenter (YuNet), audio meter, suite
  hub_client.py / hub_outbox.py / hub_sync.py  agent side of the fleet hub (enroll, durable outbox, heartbeats)
  commands.py   Telegram command router, inline keyboards, single getUpdates consumer (agent + hub)
  email_backup.py  SMTP backup route for failed urgent deliveries
  memory.py / session_report.py  Supermemory provider (scoped, key-hygienic) and broadcast/session reports
  pc_health.py / watchdog.py / clips.py / engagement.py  PC health sampling, stall detector + supervisor, GIF clips, viewer counts
  end_request.py  End streaming? dialog detection (rules/end_dialog_rules.json) and persisted end-request episodes
  perception/     automatic layout discovery (ocr_boxes, clusters, anchors, uia, layout, tracker, optional omniparser)
  popups.py / frame_analysis.py  typed popup classification first, then origin-aware broadcast evidence per frame
  audio/          AudioSourceResolver, process loopback, levels/VAD, optional transcription, AudioWorker
src/hub/        the central hub: FastAPI app, SQLAlchemy models, services, Telegram routing, dashboard templates
deploy/         Dockerfile, docker-compose.yml (PostgreSQL + hub), .env.example, deployment README
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
rules/studio_rules.json, rules/live_state_rules.json, rules/connection_rules.json; models/ (YuNet face detector)
tests/          pytest suite (fakes for Win32, capture, OCR, Telegram)
packaging/      PyInstaller spec + build script
```
