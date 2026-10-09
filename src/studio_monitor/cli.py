"""Command-line interface.

    studio-monitor                     launch the GUI
    studio-monitor list-windows        enumerate selectable windows
    studio-monitor select HWND         store a window as the Studio target
    studio-monitor run                 headless monitoring with the stored target
    studio-monitor calibrate IMAGE     OCR a real Studio screenshot and show which popup rules fire
    studio-monitor calibrate-live IMAGE  classify a real Studio screenshot as LIVE / NOT_LIVE / UNKNOWN
    studio-monitor history [--kind]    events with per-bot delivery summaries
    studio-monitor bots list|add|edit|enable|disable|remove|validate|test
    studio-monitor set-telegram        compatibility: create/update "Default Bot" (token prompted)
    studio-monitor autostart --enable|--disable|--status   start at Windows sign-in (HKCU Run key)
    studio-monitor queue               delivery counts / requeue failures

Tokens are read with a masked prompt (or --token-stdin). Passing a token as a
command-line argument is possible but discouraged: it lands in shell history
and process listings.
"""
from __future__ import annotations

import argparse
import getpass
import logging
import sys
from pathlib import Path

from . import SOURCE_LABEL, __version__
from .app import (build_monitor, load_config, load_live_rules, load_ruleset, make_registry, open_queue,
                  run_migrations, setup_logging)
from .bots import EVENT_CATEGORIES, MAX_BOTS, BotError
from .config import AppConfig
from .telegram import sanitize

TOKEN_HINT = ("Enter the bot token from @BotFather (looks like 123456789:ABC...). This is not your Telegram "
              "account password or a Telegram developer API ID/API hash.")


def _print_windows(cfg: AppConfig) -> None:
    from .win32.windows import Win32WindowSystem, looks_like_studio, selectable_windows
    system = Win32WindowSystem()
    rows = selectable_windows(system)
    print(f"{'HWND':>10}  {'PID':>6}  {'Process':<28} {'Size':<11} Title")
    for w in rows:
        mark = "*" if looks_like_studio(w) else " "
        print(f"{mark}0x{w.hwnd:08X}  {w.pid:>6}  {w.exe_name[:28]:<28} {w.rect.width}x{w.rect.height:<6} {w.title[:70]}")
    print("\n* = looks like TikTok LIVE Studio (heuristic; you still choose).")


def _select(cfg: AppConfig, cfg_path: Path, hwnd_text: str) -> int:
    from .target import identity_from_window
    from .win32.windows import Win32WindowSystem
    hwnd = int(hwnd_text, 0)
    system = Win32WindowSystem()
    win = system.get_window(hwnd)
    if win is None:
        print(f"no window with handle {hwnd_text}", file=sys.stderr)
        return 2
    if not system.process_alive(win.pid):
        print("the window's process is not running", file=sys.stderr)
        return 2
    cfg.target = identity_from_window(win, system)
    cfg.save(cfg_path)
    print(f"target stored: {win.describe()} (process started {cfg.target.process_start:.0f})")
    print(f"executable discovered: {win.exe_path}")
    return 0


def _calibrate(cfg: AppConfig, image_path: str, backend: str) -> int:
    from PIL import Image
    from .ocr import available_backends, create_backend
    from .detection.rules import normalize_text
    rules = load_ruleset(cfg)
    print(f"OCR backends available: {', '.join(available_backends()) or 'none'}")
    ocr = create_backend(backend or cfg.detection.ocr_backend, cfg.detection.ocr_language, cfg.detection.ocr_upscale)
    img = Image.open(image_path)
    regions = [r for r in cfg.regions if r.kind == "detect"] or [None]
    exit_code = 1
    for region in regions:
        crop = region.crop(img) if region else img
        text = ocr.recognize(crop).text
        name = region.name if region else "full image"
        print(f"\n=== Region: {name} ({ocr.name}) ===")
        print(text.strip() or "(no text recognised)")
        print("--- normalized ---")
        print(normalize_text(text))
        matches = rules.match_all(text)
        if matches:
            exit_code = 0
            for m in matches:
                print(f"MATCH {m.key} [{m.label}] phrases={m.phrases}"
                      f"{'  -> MANUAL ATTENTION' if m.manual_attention else ''}")
        else:
            print("no rule matched. Add the exact wording above to rules/studio_rules.json")
    return exit_code


def _calibrate_live(cfg: AppConfig, image_path: str, backend: str) -> int:
    from PIL import Image
    from .ocr import create_backend
    live_rules = load_live_rules(cfg)
    ocr = create_backend(backend or cfg.detection.ocr_backend, cfg.detection.ocr_language, cfg.detection.ocr_upscale)
    img = Image.open(image_path)
    regions = cfg.live_regions
    if regions:
        text = "\n".join(ocr.recognize(r.crop(img)).text for r in regions)
        print(f"OCR over {len(regions)} live-status region(s):")
    else:
        text = ocr.recognize(img).text
        print("OCR over the full image (no live-status regions configured):")
    print(text.strip() or "(no text recognised)")
    c = live_rules.classify(text)
    print(f"\nRESULT: {c.summary()}   (live score {c.live_score}, not-live score {c.not_live_score})")
    print("rules: " + ("verified" if live_rules.verified else "UNVERIFIED seed - set \"verified\": true in the rules "
                                                              "file once real LIVE and NOT_LIVE screenshots classify correctly"))
    return 0 if c.state.value != "UNKNOWN" else 1


def _history(cfg: AppConfig, kind: str, limit: int, expand: bool) -> int:
    from datetime import datetime
    q = open_queue(cfg)
    for it in q.history(limit, kind):
        print(f"{datetime.fromtimestamp(it['ts']):%Y-%m-%d %H:%M:%S} [{it['kind']}] {it['id']} {it['label']}: {it['detail']}")
        if expand:
            for d in q.deliveries_for(it["id"]):
                print(f"    -> {d.bot_name:<20} {d.chat_id}{'/' + str(d.thread_id) if d.thread_id else ''}  "
                      f"{d.status:<9} attempts={d.attempts} msg={d.message_id or '-'} {d.last_error}")
    print(q.delivery_status())
    return 0


def _autostart(cfg: AppConfig, cfg_path: Path, enable_: bool, disable_: bool) -> int:
    from . import startup
    if enable_:
        cmd = startup.enable()
        cfg.activity.start_at_signin = True
        cfg.save(cfg_path)
        print(f"enabled: {cmd}")
    elif disable_:
        startup.disable()
        cfg.activity.start_at_signin = False
        cfg.save(cfg_path)
        print("disabled")
    print("start at sign-in:", "enabled" if startup.is_enabled() else "disabled")
    return 0


def _run(cfg: AppConfig, cfg_path: Path, once: bool) -> int:
    if not cfg.target.is_set:
        print("no target selected; run `studio-monitor list-windows` then `select HWND`", file=sys.stderr)
        return 2
    setup_logging(cfg)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    logging.getLogger().addHandler(console)
    log = logging.getLogger("studio-monitor")

    def on_identity_change(identity):
        cfg.save(cfg_path)

    def on_status(update):
        log.debug("status %s %s", update.status.value, update.reason)

    def on_activity(a):
        log.debug("activity app=%s live=%s offline=%.0fs", a.app_state, a.live_state, a.offline_seconds)

    monitor = build_monitor(cfg, cfg_path, on_event=log.info, on_status=on_status,
                            on_identity_change=on_identity_change, on_activity=on_activity)
    enabled = [b.name for b in cfg.bots if b.enabled]
    log.info("%s monitor %s; target %s (%s); bots enabled: %s", SOURCE_LABEL, __version__, cfg.target.title,
             cfg.target.exe_name, ", ".join(enabled) or "none (alerts will queue with no deliveries)")
    if once:
        import time as _time
        deadline = _time.monotonic() + 3.0          # let the capture thread deliver its first frame
        while _time.monotonic() < deadline and monitor.frames.frame() is None:
            _time.sleep(0.1)
        dets = monitor.tick()
        st = monitor.tracker.state
        cs = monitor.frames.status()
        print(f"status: {st.status.value} {st.reason}")
        print(f"capture: {cs.health} backend={cs.backend or '-'} frames={cs.frames}"
              f"{(' reason=' + cs.reason) if cs.reason else ''}")
        a = monitor.activity
        print(f"studio: {a.app_state}  broadcast: {a.live_state} ({a.last_observation or '-'})"
              f"{'' if a.live_rules_verified else '  [live rules unverified]'}")
        monitor.frames.stop()
        for d in dets:
            print(f"detection: {d.category} in {'dialog' if d.is_dialog else 'main'}: {d.ocr_text[:120]!r}")
        return 0
    try:
        monitor.run()
    except KeyboardInterrupt:
        monitor.stop()
    return 0


# ---------------------------------------------------------------- bots

def _read_token(args) -> str:
    if getattr(args, "token", None):
        print("warning: a token passed as an argument is visible in shell history and process listings; "
              "prefer the masked prompt or --token-stdin", file=sys.stderr)
        return args.token.strip()
    if getattr(args, "token_stdin", False):
        return sys.stdin.readline().strip()
    print(TOKEN_HINT)
    return getpass.getpass("Bot token (input hidden): ").strip()


def _registry(cfg, cfg_path):
    queue = open_queue(cfg)
    reg = make_registry(cfg, cfg_path, queue)
    for note in run_migrations(cfg, cfg_path, reg, queue):
        print(f"note: {note}")
    return reg, queue


def _find_bot(reg, ref: str):
    bot = reg.get(ref) or reg.by_name(ref)
    if bot is None:
        raise BotError(f"no bot named or identified by {ref!r}; use `bots list`")
    return bot


def _subs(text: str | None) -> list[str] | None:
    if text is None:
        return None
    if text.strip().lower() in ("all", "*"):
        return list(EVENT_CATEGORIES)
    return [s.strip() for s in text.split(",") if s.strip()]


def _bots(cfg: AppConfig, cfg_path: Path, args) -> int:
    from .bot_tests import deliver_test_now, enqueue_test, validate_bot, validate_token
    from .telegram import ClientFactory
    reg, queue = _registry(cfg, cfg_path)
    factory = ClientFactory(cfg.telegram, reg.token_for)
    sub = args.bots_cmd
    try:
        if sub == "list":
            print(f"Bots: {reg.count} / {MAX_BOTS}")
            for b in reg.bots:
                st = queue.bot_stats(b.bot_id)
                print(f"- {b.name}  [{'enabled' if b.enabled else 'disabled'}]  id={b.bot_id}")
                print(f"    telegram: {('@' + b.verified_username) if b.verified_username else 'not validated'}"
                      f"  destination: {b.destination}  subscriptions: {', '.join(b.subscriptions) or '-'}")
                print(f"    last test: {b.last_test_result or '-'}  last delivery: {st['last_result'] or '-'}"
                      f"  pending: {st['pending']}  credential: {b.credential_ref}")
            if not reg.bots:
                print("no bots configured. Add one with: studio-monitor bots add --name NAME --chat-id ID")
            return 0
        if sub == "add":
            if not reg.can_add:
                print(f"maximum of {MAX_BOTS} bots reached (disabled bots count); remove one first", file=sys.stderr)
                return 2
            token = _read_token(args)
            verified = None
            if not args.no_validate:
                info = validate_token(factory, token)
                verified = (info["id"], info["username"])
                print(f"token valid: @{info['username']} (id {info['id']})")
            bot = reg.add(args.name, token, args.chat_id, args.topic, not args.disabled, _subs(args.subscribe), verified)
            print(f"added bot '{bot.name}' id={bot.bot_id} -> {bot.destination}; token stored at {bot.credential_ref}")
            return 0
        bot = _find_bot(reg, args.bot)
        if sub == "edit":
            kwargs = {}
            if args.name:
                kwargs["name"] = args.name
            if args.chat_id:
                kwargs["chat_id"] = args.chat_id
            if args.topic is not None:
                kwargs["thread_id"] = args.topic or None
            if args.subscribe is not None:
                kwargs["subscriptions"] = _subs(args.subscribe)
            if args.rotate_token or args.token or args.token_stdin:
                token = _read_token(args)
                info = validate_token(factory, token)
                kwargs["new_token"] = token
                kwargs["new_token_identity"] = (info["id"], info["username"])
            reg.update(bot.bot_id, **kwargs)
            print(f"updated '{bot.name}' (destination changes apply to future events only)")
            return 0
        if sub in ("enable", "disable"):
            reg.set_enabled(bot.bot_id, sub == "enable")
            print(f"{bot.name}: {sub}d" + ("; pending deliveries cancelled" if sub == "disable" else ""))
            return 0
        if sub == "remove":
            pend = queue.bot_stats(bot.bot_id)["pending"]
            if not args.yes:
                ans = input(f"Remove bot '{bot.name}' ({bot.destination})? Its {pend} pending delivery(ies) will be "
                            f"cancelled and its stored token deleted. [y/N] ")
                if ans.strip().lower() not in ("y", "yes"):
                    print("cancelled")
                    return 1
            reg.remove(bot.bot_id)
            print(f"removed '{bot.name}'; history kept, credential deleted")
            return 0
        if sub == "validate":
            info = validate_bot(factory, reg, bot.bot_id)
            print(f"token valid: @{info['username']} (id {info['id']}, {info['first_name']}). "
                  "This checks the token only; whether the bot may post to the destination is tested by `bots test`.")
            return 0
        if sub == "test":
            event_id = enqueue_test(queue, reg, cfg, bot.bot_id)
            print(deliver_test_now(queue, reg, factory, event_id))
            return 0
    except BotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: {sanitize(str(exc))}", file=sys.stderr)
        return 2
    return 1


def _set_telegram(cfg: AppConfig, cfg_path: Path, args) -> int:
    """Compatibility path: creates or updates 'Default Bot'."""
    reg, queue = _registry(cfg, cfg_path)
    token = _read_token(args)
    try:
        bot = reg.by_name("Default Bot")
        if bot is None:
            bot = reg.add("Default Bot", token, args.chat_id)
            print(f"created 'Default Bot' -> {bot.destination}")
        else:
            reg.update(bot.bot_id, chat_id=args.chat_id, new_token=token if not bot.verified_bot_id else None)
            if bot.verified_bot_id:
                print("Default Bot is validated; rotate its token with `bots edit \"Default Bot\" --rotate-token`")
            print(f"updated 'Default Bot' -> {bot.destination}")
    except BotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print("note: set-telegram is kept for compatibility; manage bots with `studio-monitor bots ...`")
    return 0


def _engine(cfg: AppConfig):
    from .incident_engine import IncidentEngine
    q = open_queue(cfg)
    cfg.ensure_device_id()
    return q, IncidentEngine(q._conn, q._lock)


def _maintenance(cfg: AppConfig, cfg_path: Path, args) -> int:
    q, eng = _engine(cfg)
    dev = cfg.device.device_id
    if args.action == "enter":
        if not args.minutes:
            print("minutes required", file=sys.stderr); return 2
        cats = [c.strip() for c in args.categories.split(",") if c.strip()]
        until = eng.enter_maintenance(dev, args.minutes * 60, cats, args.reason)
        from datetime import datetime
        print(f"maintenance until {datetime.fromtimestamp(until):%H:%M:%S}; suppressed: {', '.join(eng.maintenance(dev)['categories'])} "
              "(restrictions and verification stay enabled)")
        return 0
    if args.action == "exit":
        print("maintenance ended" if eng.exit_maintenance(dev) else "no maintenance active"); return 0
    m = eng.maintenance(dev)
    print("maintenance:", f"{m['remaining_seconds']/60:.0f} min left, suppressing {m['categories']}" if m else "not active")
    return 0


def _incidents(cfg: AppConfig, args) -> int:
    q, eng = _engine(cfg)
    a = args.args
    if args.action == "list":
        for inc in eng.list(limit=50):
            print(f"{inc.incident_id} {inc.status:<8} {inc.severity:<7} ack={'y' if inc.acknowledged else 'n'} x{inc.occurrences} "
                  f"{inc.category}/{inc.problem_key} {inc.opened_utc} {inc.summary[:70]}")
        return 0
    if not a:
        print("incident id required", file=sys.stderr); return 2
    if args.action == "ack":
        inc = eng.acknowledge(a[0], actor="cli", note=" ".join(a[1:])); print("acknowledged" if inc and inc.acknowledged else "not found"); return 0
    if args.action == "resolve":
        inc = eng.resolve(a[0], " ".join(a[1:]) or "resolved by operator", actor="cli"); print(inc.status if inc else "not found"); return 0
    if args.action == "snooze":
        until = eng.snooze("incident", a[0], float(a[1]) * 60 if len(a) > 1 else 1800, actor="cli"); print(f"snoozed until {until:.0f}"); return 0
    for t in eng.timeline(a[0]):
        print(f"{t['ts_utc']} {t['kind']:<12} x{t['coalesced']} {t['text'][:100]} {t['actor']}")
    return 0


def _schedule(cfg: AppConfig, cfg_path: Path, args) -> int:
    from .schedules import Schedule
    s = cfg.schedule
    if args.enable: s.enabled = True
    if args.disable: s.enabled = False
    if args.tz: s.timezone = args.tz
    if args.days: s.weekdays = [d.strip().lower()[:3] for d in args.days.split(",")]
    if args.start: s.start = args.start
    if args.end: s.end = args.end
    if args.grace is not None: s.grace_minutes = args.grace
    errs = s.validate()
    if errs:
        print("invalid schedule: " + "; ".join(errs), file=sys.stderr); return 2
    cfg.device.schedule = s.to_dict(); cfg.save(cfg_path)
    print("schedule:", s.describe())
    return 0


def _account(cfg: AppConfig, cfg_path: Path, action: str) -> int:
    from .account import IdentityStore, AccountIdentity, LookupContext, Win32Interactor, perform_lookup
    q = open_queue(cfg)
    store = IdentityStore(q)
    if action == "clear":
        store.save(AccountIdentity())
        print("stored account identity cleared")
        return 0
    if action == "test":
        if not cfg.target.is_set:
            print("select the Studio window first (list-windows / select)", file=sys.stderr)
            return 2
        from .win32.windows import Win32WindowSystem
        from .win32.capture import CaptureService
        from .ocr import create_backend
        print("This opens the Studio profile menu once (guarded: Studio must be idle and in front). Reading...")
        system = Win32WindowSystem()
        svc = CaptureService(system, interval=0.3); svc.bind(cfg.target.hwnd)
        import time as _t; _t.sleep(1.5)
        ocr = create_backend(cfg.detection.ocr_backend, cfg.detection.ocr_language, cfg.detection.ocr_upscale)
        a = cfg.account
        ctx = LookupContext(system=system, interactor=Win32Interactor(system), identity=cfg.target,
                            ocr=lambda img: ocr.recognize(img).text,
                            fresh_frame=lambda: (f.image if (f := svc.frame(max_age=float("inf"))) else None),
                            profile_region=cfg.profile_region, offset_right=a.profile_offset_right,
                            offset_top=a.profile_offset_top, idle_required=a.idle_seconds, timeout=a.timeout_seconds,
                            allow_physical=a.allow_physical_click, log=print)
        res = perform_lookup(ctx)
        svc.stop()
        print(f"result: {res.status} username={('@' + res.username) if res.username else '-'} display={res.display_name or '-'} "
              f"source={res.source or '-'} error={res.error or '-'} attempts={res.attempts}")
        print("steps:", "; ".join(res.steps))
        return 0 if res.status == "SUCCEEDED" else 1
    ident = store.load()
    print(f"status={ident.status} username={ident.handle or '-'} display_name={ident.display_name or '-'} "
          f"source={ident.source or '-'} observed={ident.observed_utc or '-'} episode={ident.episode_id or '-'} "
          f"attempts={ident.attempts} error={ident.error or '-'}")
    return 0


def _test_alert(cfg: AppConfig, cfg_path: Path | None = None) -> int:
    """Send a test notification through every enabled bot (GUI 'Test Telegram')."""
    from .bot_tests import deliver_test_now, enqueue_test
    from .telegram import ClientFactory
    from .config import default_config_path
    reg, queue = _registry(cfg, cfg_path or default_config_path())
    factory = ClientFactory(cfg.telegram, reg.token_for)
    bots = [b for b in reg.bots if b.enabled]
    if not bots:
        print("no enabled bots configured (Telegram Bots tab / `bots add`)", file=sys.stderr)
        return 2
    rc = 0
    for b in bots:
        res = deliver_test_now(queue, reg, factory, enqueue_test(queue, reg, cfg, b.bot_id))
        print(f"{b.name}: {res}")
        if "failed" in res:
            rc = 1
    return rc


def _utf8_console() -> None:
    """Window titles can contain characters the legacy console code page cannot
    encode (e.g. U+200E in a Chrome tab title); never crash on printing them."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _memory(cfg: AppConfig, action: str, query: str) -> int:
    from .app import make_memory_provider
    from .credentials import default_store
    from .memory import MEMORY_CRED_KEY, format_hits, load_api_key
    store = default_store()
    if action == "set-key":
        key = getpass.getpass("Supermemory API key (stored in the Windows Credential Manager, never in settings): ") \
            if sys.stdin.isatty() else sys.stdin.readline().strip()
        if not key:
            print("no key entered"); return 2
        store.set(MEMORY_CRED_KEY, key)
        print("Supermemory key stored")
        return 0
    if action == "clear-key":
        store.delete(MEMORY_CRED_KEY)
        print("Supermemory key removed")
        return 0
    has_key = bool(load_api_key(store))
    print(f"memory enabled={cfg.memory.enabled} mode={cfg.device.mode} key={'stored' if has_key else 'missing'} "
          f"namespace={cfg.memory.namespace or '(default per device)'} sync incidents={cfg.memory.sync_resolved_incidents} "
          f"reports={cfg.memory.sync_reports}")
    if cfg.device.mode == "managed":
        print("managed mode: the hub syncs memory with its own key; this PC never receives it")
    if action == "search":
        if not query:
            print("QUERY is required"); return 2
        p = make_memory_provider(cfg, store)
        if p is None:
            print("memory not available (disabled, managed mode, or key missing -> `memory set-key`)"); return 1
        cfg.ensure_device_id()
        hits = p.search(query, {"device_id": cfg.device.device_id}, cfg.memory.retrieval_limit)
        print(format_hits(hits).replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "") or "no results")
        if getattr(p, "last_error", ""):
            print(f"error: {p.last_error}")
    return 0


def _smtp(cfg: AppConfig, action: str) -> int:
    from .app import make_email_backup
    from .credentials import default_store
    from .email_backup import SMTP_CRED_KEY
    s = cfg.smtp
    if action == "set-password":
        pw = getpass.getpass("SMTP password: ") if sys.stdin.isatty() else sys.stdin.readline().strip()
        default_store().set(SMTP_CRED_KEY, pw)
        print("SMTP password stored in the Windows Credential Manager")
        return 0
    print(f"e-mail backup enabled={s.enabled} host={s.host or '-'}:{s.port} user={s.username or '-'} from={s.from_addr or '-'} "
          f"to={','.join(s.to_addrs) or '-'} starttls={s.starttls} min_severity={s.min_severity} "
          f"password={'stored' if default_store().get(SMTP_CRED_KEY) else 'missing'}")
    if action == "test":
        if not s.enabled:
            print("enable it in Settings first"); return 1
        eb = make_email_backup(cfg)
        ok = eb.send(f"[{cfg.notification_label}] e-mail backup test", "This is an explicit test of the SMTP backup route.")
        print("sent" if ok else f"failed: {eb.last_error}")
        return 0 if ok else 1
    return 0


def _hub(cfg: AppConfig, cfg_path: Path, action: str, url: str, code: str, mode: str) -> int:
    from .app import enroll_agent, make_hub_sync, unenroll_agent
    from .hub_client import HubClientError
    if action == "enroll":
        url = url or cfg.hub.url
        if not url:
            print("--url is required (e.g. https://hub.example.org)"); return 2
        if not code:
            code = getpass.getpass("Pairing code (single use): ") if sys.stdin.isatty() else sys.stdin.readline().strip()
        try:
            res = enroll_agent(cfg, cfg_path, url, code, mode or None)
        except HubClientError as exc:
            print(f"enrollment failed: {sanitize(str(exc))}"); return 1
        print(f"enrolled device {res['device_id']} in workspace {res['workspace_id']} ({res['mode']} mode); "
              f"heartbeat every {res['heartbeat_interval']} s, unreachable after {res['unreachable_after']} s. "
              "The agent credential is stored in the Windows Credential Manager.")
        return 0
    if action == "unenroll":
        unenroll_agent(cfg, cfg_path)
        print("enrollment removed (agent credential deleted; the hub keeps the device record until revoked there)")
        return 0
    if not cfg.device.device_id or not cfg.device.install_fingerprint:
        cfg.ensure_device_id()
        cfg.save(cfg_path)                      # persist the identity so it stays stable across runs
    else:
        cfg.ensure_device_id()
    h = cfg.hub
    print(f"device_id: {cfg.device.device_id}  name: {cfg.device.device_name}  mode: {cfg.device.mode}")
    print(f"hub: {h.url or '(none)'}  enrolled: {h.enrolled}  workspace: {h.workspace_id or '-'}  since: {h.enrolled_utc or '-'}")
    if not (h.url and h.enrolled):
        return 0
    sync = make_hub_sync(cfg, on_event=print)
    if sync is None:
        print("agent credential missing from the credential store; run `hub enroll` again"); return 1
    try:
        if action == "sync-once":
            sync.tick()
        else:
            try:
                sync.client.health()
                print("hub reachable")
            except HubClientError as exc:
                print(f"hub not reachable: {sanitize(str(exc))}")
        c = sync.outbox.counts()
        print(f"outbox: pending={c['pending']} evidence={c['evidence']} done={c['done']} rejected={c['rejected']} "
              f"last_error={sync.outbox.last_error() or '-'}")
        st = sync.status
        print(f"sync: connected={st.connected} last_error={st.last_error or '-'}")
    finally:
        sync.client.close()
    return 0


def _detectors(cfg: AppConfig, action: str, image: str | None, backend: str) -> int:
    """Operator calibration helpers for the stream-health detectors. Never changes settings."""
    from PIL import Image
    from .config import connection_rules_path
    from .detectors.presenter import YuNetDetector, model_path, verify_model
    from .detectors.text_rules import ConnectionRules
    d = cfg.detectors
    if action == "status":
        print(f"detectors enabled: {d.enabled}; presenter: {d.presenter_enabled} (expected on camera: {d.presenter_expected}); "
              f"audio meter: {d.audio_enabled} ({d.audio_profile})")
        print(f"face model: {model_path()} -> {'OK (checksum verified)' if verify_model(model_path()) else 'MISSING / checksum mismatch'}")
        rules = ConnectionRules.load(connection_rules_path(cfg))
        print(f"connection rules: {connection_rules_path(cfg)} verified={rules.verified} "
              f"({len(rules.reconnecting)} reconnecting, {len(rules.ended)} ended, {len(rules.source_missing)} source phrases)")
        print("regions: " + (", ".join(f"[{r.kind}] {r.name}" for r in cfg.regions if r.kind in ("face", "audio")) or "no presenter/audio regions"))
        print(f"thresholds: face absent {d.face_absent_seconds:g}s, still {d.motion_low_seconds:g}s, frozen {d.frozen_seconds:g}s, "
              f"black {d.black_preview_seconds:g}s, connection {d.connection_sustain_seconds:g}s, audio {d.audio_silence_seconds:g}s")
        return 0
    if not image:
        print("IMAGE is required for this action"); return 2
    img = Image.open(image).convert("RGB")
    if action == "text":
        ocr = create_backend(backend or cfg.detection.ocr_backend, cfg.detection.ocr_language, cfg.detection.ocr_upscale)
        text = ocr.recognize(img).text
        cls = ConnectionRules.load(connection_rules_path(cfg)).classify(text)
        print(f"OCR text ({len(text)} chars): {' '.join(text.split())[:400]!r}")
        print("classification: " + ", ".join(f"{k}={v!r}" for k, v in cls.items()))
        return 0
    region = next((r for r in cfg.regions if r.kind == ("face" if action == "face" else "audio")), None)
    crop = region.crop(img) if region else img
    print(f"region: {region.name if region else 'whole image (no region configured)'} size={crop.size}")
    if action == "face":
        det = YuNetDetector()
        faces = det.detect(crop)
        print(f"faces detected: {len(faces)}")
        for f in faces:
            print(f"  box={f.box} score={f.score:.2f}")
        return 0
    from .detectors.audio import read_meter
    r = read_meter(img, region.to_box(*img.size) if region else (0, 0, img.width, img.height))
    print(f"audio meter readable={r.valid} level={r.level:.3f} {r.note}")
    return 0


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    parser = argparse.ArgumentParser(prog="studio-monitor", description=f"{SOURCE_LABEL} popup monitor")
    parser.add_argument("--config", type=Path, help="config file path")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("gui"); p.add_argument("--autostart", action="store_true", help="begin monitoring the saved target")
    sub.add_parser("list-windows")
    p = sub.add_parser("select"); p.add_argument("hwnd")
    p = sub.add_parser("run"); p.add_argument("--once", action="store_true")
    p = sub.add_parser("calibrate"); p.add_argument("image"); p.add_argument("--backend", default="")
    p = sub.add_parser("calibrate-live"); p.add_argument("image"); p.add_argument("--backend", default="")
    p = sub.add_parser("history"); p.add_argument("--kind", choices=["all", "incident", "activity"], default="all")
    p.add_argument("--limit", type=int, default=50); p.add_argument("--expand", action="store_true")
    p = sub.add_parser("autostart"); g = p.add_mutually_exclusive_group()
    g.add_argument("--enable", action="store_true"); g.add_argument("--disable", action="store_true")
    g.add_argument("--status", action="store_true")
    sub.add_parser("test-alert")
    p = sub.add_parser("maintenance", help="break/maintenance mode: enter MINUTES [--categories a,b] | exit | status")
    p.add_argument("action", choices=["enter", "exit", "status"]); p.add_argument("minutes", nargs="?", type=float)
    p.add_argument("--categories", default="face,audio,source,reminders,studio_opened,studio_closed,health")
    p.add_argument("--reason", default="")
    p = sub.add_parser("incidents", help="durable incidents: list | ack ID | resolve ID TEXT | snooze ID MINUTES | timeline ID")
    p.add_argument("action", choices=["list", "ack", "resolve", "snooze", "timeline"]); p.add_argument("args", nargs="*")
    p = sub.add_parser("schedule", help="show or set the streaming schedule")
    p.add_argument("--enable", action="store_true"); p.add_argument("--disable", action="store_true")
    p.add_argument("--tz"); p.add_argument("--days"); p.add_argument("--start"); p.add_argument("--end"); p.add_argument("--grace", type=int)
    p = sub.add_parser("hub", help="fleet hub: enroll --url URL --code CODE [--mode managed|standalone] | status | unenroll | sync-once")
    p.add_argument("action", choices=["enroll", "status", "unenroll", "sync-once"])
    p.add_argument("--url", default=""); p.add_argument("--code", default=""); p.add_argument("--mode", default="")
    p = sub.add_parser("supervise", help="run the monitor under a restarting supervisor (watchdog): supervise [--gui]")
    p.add_argument("--gui", action="store_true", help="supervise the GUI with --autostart instead of the headless runner")
    sub.add_parser("doctor", help="environment checks (credential store, OCR, capture, model, disk, target, bots, hub)")
    p = sub.add_parser("memory", help="Supermemory: set-key | clear-key | status | search QUERY")
    p.add_argument("action", choices=["set-key", "clear-key", "status", "search"]); p.add_argument("query", nargs="?", default="")
    p = sub.add_parser("smtp", help="e-mail backup route: set-password | status | test")
    p.add_argument("action", choices=["set-password", "status", "test"])
    p = sub.add_parser("detectors", help="stream-health detectors: status | text IMAGE | face IMAGE | audio IMAGE")
    p.add_argument("action", choices=["status", "text", "face", "audio"]); p.add_argument("image", nargs="?")
    p.add_argument("--backend", default="")
    p = sub.add_parser("account", help="TikTok account discovery: status | test | clear")
    p.add_argument("action", choices=["status", "test", "clear"], nargs="?", default="status")
    p = sub.add_parser("owner", help="show or set the PC owner name used in notification labels")
    p.add_argument("name", nargs="?", help="owner name (omit to show)")
    p.add_argument("--clear", action="store_true", help="clear the owner name (fall back to the machine label)")
    p = sub.add_parser("set-telegram"); p.add_argument("--chat-id", required=True)
    p.add_argument("--token", help="discouraged: visible in shell history"); p.add_argument("--token-stdin", action="store_true")
    p = sub.add_parser("queue"); p.add_argument("--requeue-failed", action="store_true")

    bp = sub.add_parser("bots", help="manage Telegram bots")
    bs = bp.add_subparsers(dest="bots_cmd", required=True)
    bs.add_parser("list")
    a = bs.add_parser("add"); a.add_argument("--name", required=True); a.add_argument("--chat-id", required=True)
    a.add_argument("--topic", default=None, help="forum topic id (message_thread_id)")
    a.add_argument("--subscribe", default=None, help="comma list or 'all' (default all): " + ",".join(EVENT_CATEGORIES))
    a.add_argument("--disabled", action="store_true"); a.add_argument("--no-validate", action="store_true")
    a.add_argument("--token", help="discouraged"); a.add_argument("--token-stdin", action="store_true")
    for name in ("edit", "enable", "disable", "remove", "validate", "test"):
        q = bs.add_parser(name); q.add_argument("bot", help="bot name or id")
        if name == "edit":
            q.add_argument("--name"); q.add_argument("--chat-id"); q.add_argument("--topic", default=None)
            q.add_argument("--subscribe", default=None); q.add_argument("--rotate-token", action="store_true")
            q.add_argument("--token", help="discouraged"); q.add_argument("--token-stdin", action="store_true")
        if name == "remove":
            q.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)

    cfg, cfg_path = load_config(args.config)
    cmd = args.cmd or "gui"
    if cmd == "gui":
        from .gui.app import run_gui
        return run_gui(cfg, cfg_path, autostart=getattr(args, "autostart", False))
    if cmd == "list-windows":
        _print_windows(cfg)
        return 0
    if cmd == "select":
        return _select(cfg, cfg_path, args.hwnd)
    if cmd == "run":
        return _run(cfg, cfg_path, args.once)
    if cmd == "calibrate":
        return _calibrate(cfg, args.image, args.backend)
    if cmd == "calibrate-live":
        return _calibrate_live(cfg, args.image, args.backend)
    if cmd == "history":
        return _history(cfg, args.kind, args.limit, args.expand)
    if args.cmd == "detectors":
        return _detectors(cfg, args.action, args.image, args.backend)
    if args.cmd == "hub":
        return _hub(cfg, cfg_path, args.action, args.url, args.code, args.mode)
    if args.cmd == "smtp":
        return _smtp(cfg, args.action)
    if args.cmd == "memory":
        return _memory(cfg, args.action, args.query)
    if args.cmd == "doctor":
        from .app import doctor
        worst = 0
        for name, status, detail in doctor(cfg, cfg_path):
            print(f"[{status:<4}] {name}: {detail}")
            worst = max(worst, {"OK": 0, "WARN": 1, "FAIL": 2}[status])
        return worst
    if args.cmd == "supervise":
        from .watchdog import Supervisor, default_spawn
        argv = ["gui", "--autostart"] if args.gui else ["run"]
        print(f"supervising `studio-monitor {' '.join(argv)}` (restarts on crash, max 10/hour; Ctrl+C to stop)")
        sup = Supervisor(default_spawn(argv), on_event=print)
        try:
            state = sup.run()
        except KeyboardInterrupt:
            sup.stop(); state = sup.state
        return 1 if state.gave_up else 0
    if cmd == "autostart":
        return _autostart(cfg, cfg_path, args.enable, args.disable)
    if cmd == "maintenance":
        return _maintenance(cfg, cfg_path, args)
    if cmd == "incidents":
        return _incidents(cfg, args)
    if cmd == "schedule":
        return _schedule(cfg, cfg_path, args)
    if cmd == "account":
        return _account(cfg, cfg_path, args.action)
    if cmd == "owner":
        from .labels import OwnerNameError, validate_owner_name
        if args.clear:
            cfg.owner_name = ""
            cfg.save(cfg_path)
        elif args.name is not None:
            try:
                cfg.owner_name = validate_owner_name(args.name)
            except OwnerNameError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            cfg.save(cfg_path)
        print(f"owner: {cfg.owner_name or '(blank -> machine label)'}  notification label: {cfg.notification_label}")
        return 0
    if cmd == "test-alert":
        return _test_alert(cfg, cfg_path)
    if cmd == "set-telegram":
        return _set_telegram(cfg, cfg_path, args)
    if cmd == "bots":
        return _bots(cfg, cfg_path, args)
    if cmd == "queue":
        q = open_queue(cfg)
        if args.requeue_failed:
            print(f"requeued {q.requeue_failed()} failed/dead delivery(ies)")
        print(q.counts())
        return 0
    parser.print_help()
    return 1
