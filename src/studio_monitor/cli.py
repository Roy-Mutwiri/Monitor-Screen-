"""Command-line interface.

    studio-monitor                     launch the GUI
    studio-monitor list-windows        enumerate selectable windows
    studio-monitor select HWND         store a window as the Studio target
    studio-monitor run                 headless monitoring with the stored target
    studio-monitor calibrate IMAGE     OCR a real Studio screenshot and show which rules fire
    studio-monitor test-alert          send a test alert through the delivery queue
    studio-monitor set-telegram        store bot token / chat id
    studio-monitor queue               show delivery queue counts / requeue failures
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from . import SOURCE_LABEL, __version__
from .app import build_monitor, load_config, load_ruleset, setup_logging
from .config import AppConfig


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
    cfg.target = identity_from_window(win)
    cfg.save(cfg_path)
    print(f"target stored: {win.describe()}")
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

    monitor = build_monitor(cfg, on_event=log.info, on_status=on_status, on_identity_change=on_identity_change)
    log.info("%s monitor %s; target %s (%s)", SOURCE_LABEL, __version__, cfg.target.title, cfg.target.exe_name)
    if once:
        dets = monitor.tick()
        st = monitor.tracker.state
        print(f"status: {st.status.value} {st.reason}")
        for d in dets:
            print(f"detection: {d.category} in {'dialog' if d.is_dialog else 'main'}: {d.ocr_text[:120]!r}")
        return 0
    try:
        monitor.run()
    except KeyboardInterrupt:
        monitor.stop()
    return 0


def _test_alert(cfg: AppConfig) -> int:
    from .alerts import format_alert
    from .incidents import Incident, new_incident_id
    from .queue import DeliveryQueue, DeliveryWorker
    from .telegram import TelegramClient, make_sender
    client = TelegramClient(cfg.telegram)
    if not client.configured:
        print("Telegram is not configured (set-telegram or env vars)", file=sys.stderr)
        return 2
    now = time.time()
    inc = Incident(new_incident_id(), "test", "Test alert", "This is a test alert from the monitor.",
                   "test", now, now, last_alerted=now, window_title="(test)")
    payload = format_alert(inc, cfg.machine_label, screenshot_attached=False, reason="test alert")
    queue = DeliveryQueue(cfg.db_path)
    queue.enqueue(inc.incident_id, payload, "")
    worker = DeliveryWorker(queue, make_sender(client), on_event=print)
    worker.process_once()
    print(queue.counts())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="studio-monitor", description=f"{SOURCE_LABEL} popup monitor")
    parser.add_argument("--config", type=Path, help="config file path")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("gui")
    sub.add_parser("list-windows")
    p = sub.add_parser("select"); p.add_argument("hwnd")
    p = sub.add_parser("run"); p.add_argument("--once", action="store_true")
    p = sub.add_parser("calibrate"); p.add_argument("image"); p.add_argument("--backend", default="")
    sub.add_parser("test-alert")
    p = sub.add_parser("set-telegram"); p.add_argument("--token", required=True); p.add_argument("--chat-id", required=True)
    p = sub.add_parser("queue"); p.add_argument("--requeue-failed", action="store_true")
    args = parser.parse_args(argv)

    cfg, cfg_path = load_config(args.config)
    cmd = args.cmd or "gui"
    if cmd == "gui":
        from .gui.app import run_gui
        return run_gui(cfg, cfg_path)
    if cmd == "list-windows":
        _print_windows(cfg)
        return 0
    if cmd == "select":
        return _select(cfg, cfg_path, args.hwnd)
    if cmd == "run":
        return _run(cfg, cfg_path, args.once)
    if cmd == "calibrate":
        return _calibrate(cfg, args.image, args.backend)
    if cmd == "test-alert":
        return _test_alert(cfg)
    if cmd == "set-telegram":
        cfg.telegram.bot_token = args.token
        cfg.telegram.chat_id = args.chat_id
        cfg.save(cfg_path)
        print(f"saved to {cfg_path}")
        return 0
    if cmd == "queue":
        from .queue import DeliveryQueue
        q = DeliveryQueue(cfg.db_path)
        if args.requeue_failed:
            print(f"requeued {q.requeue_failed()} failed alert(s)")
        print(q.counts())
        return 0
    parser.print_help()
    return 1
