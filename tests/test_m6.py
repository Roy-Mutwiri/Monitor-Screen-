"""Milestone 6 — PC health (fake psutil), loop watchdog + process supervisor,
optional incident clips, engagement OCR, secret redaction, doctor."""
from __future__ import annotations

import types
from pathlib import Path

import pytest
from PIL import Image

from conftest import FakeClock, FakeTransport, all_deliveries, fake_frame
from studio_monitor.clips import ClipBuffer, ClipsConfig
from studio_monitor.config import AppConfig, TelegramConfig
from studio_monitor.engagement import EngagementStats, parse_engagement
from studio_monitor.pc_health import PcHealthConfig, PcHealthSampler
from studio_monitor.telegram import TelegramClient, deliver, sanitize
from studio_monitor.watchdog import StallDetector, Supervisor, SupervisorPolicy
from test_activity import Harness, LIVE_TEXT, NOT_LIVE_TEXT

RESTRICTION = "Your LIVE was ended due to a violation of our Community Guidelines"


class FakePsutil:
    def __init__(self):
        self.cpu, self.mem, self.disk_used, self.battery, self.plugged = 20.0, 40.0, 50.0, None, True
        self.sent, self.recv = 0, 0
        self.proc_cpu, self.proc_rss = 12.0, 800 * 1024 * 1024

    def cpu_percent(self, interval=None): return self.cpu
    def virtual_memory(self): return types.SimpleNamespace(percent=self.mem)
    def disk_usage(self, path): return types.SimpleNamespace(percent=self.disk_used)
    def sensors_battery(self):
        return None if self.battery is None else types.SimpleNamespace(percent=self.battery, power_plugged=self.plugged)
    def net_io_counters(self): return types.SimpleNamespace(bytes_sent=self.sent, bytes_recv=self.recv)

    def Process(self, pid):
        ps = self
        class P:
            def cpu_percent(self, interval=None): return ps.proc_cpu
            def memory_info(self): return types.SimpleNamespace(rss=ps.proc_rss)
        return P()


def sampler(clock, **over):
    cfg = PcHealthConfig(interval_seconds=30, sustain_seconds=120, recover_seconds=60, **over)
    ps = FakePsutil()
    return PcHealthSampler(cfg, "D:/data", ps=ps, clock=clock, mono=clock), ps


# ---------------------------------------------------------------- pc health

def test_pc_health_sustained_threshold_and_recovery():
    clock = FakeClock()
    s, ps = sampler(clock, upload_kbps_min=500)
    assert s.due()
    smp = s.sample(studio_pid=4242)
    assert smp.cpu_percent == 20 and smp.studio_memory_mb == 800 and smp.upload_kbps is None    # first net sample has no rate
    assert s.evaluate(smp, live=True) == ([], [])
    assert not s.due()
    ps.cpu = 97.0
    confirmed = []
    for _ in range(5):                          # 150 s of high CPU -> confirmed once
        clock.advance(30)
        ps.sent += 1_000_000                    # 1 MB / 30 s = 266 kbps < 500 -> UPLOAD_LOW too (live)
        c, r = s.evaluate(s.sample(), live=True)
        confirmed += [n for n, _ in c]
    assert confirmed.count("CPU_HIGH") == 1 and "UPLOAD_LOW" in confirmed
    assert "95" in dict(s.evaluate.__self__.cond)["CPU_HIGH"].last_evidence or s.cond["CPU_HIGH"].confirmed
    ps.cpu = 30.0
    recovered = []
    for _ in range(3):
        clock.advance(30)
        ps.sent += 100_000_000                 # ~26 Mbps -> upload healthy again
        c, r = s.evaluate(s.sample(), live=True)
        recovered += [n for n, _ in r]
    assert "CPU_HIGH" in recovered and "UPLOAD_LOW" in recovered
    # battery only counts when discharging; disk low needs the data drive
    ps.battery, ps.plugged = 10.0, True
    c, _ = s.evaluate(s.sample(), live=False)
    assert not any(n == "BATTERY_LOW" for n, _ in c) and s.cond["BATTERY_LOW"].active_since is None
    ps.plugged = False
    for _ in range(5):
        clock.advance(30)
        c, _ = s.evaluate(s.sample(), live=False)
    assert s.cond["BATTERY_LOW"].confirmed
    assert s.snapshot()["problems"] == ["BATTERY_LOW"]


def test_monitor_raises_and_resolves_pc_health_incident(cfg, rules, clock):
    h = Harness(cfg, rules, clock)
    ps = FakePsutil()
    h.mon.pc_health = PcHealthSampler(PcHealthConfig(interval_seconds=10, sustain_seconds=30, recover_seconds=20), cfg.data_dir,
                                      ps=ps, clock=clock, mono=clock)
    ps.mem = 95.0
    h.run(60)
    alerts = [d for d in all_deliveries(h.queue) if d["event_id"].startswith("PCH-") and d["event_id"].count("-") == 3]
    assert len(alerts) == 1 and "PC HEALTH: MEMORY HIGH" in alerts[0]["payload"]["text"] and "memory 95%" in alerts[0]["payload"]["text"]
    inc = h.mon.incident_engine.get(alerts[0]["event_id"])
    assert inc is not None and inc.is_open and inc.category == "health"
    ps.mem = 40.0
    h.run(60)
    res = [d for d in all_deliveries(h.queue) if d["event_id"].endswith("-RES") and d["event_id"].startswith("PCH-")]
    assert len(res) == 1 and "PC HEALTH OK" in res[0]["payload"]["text"]
    assert not h.mon.incident_engine.get(alerts[0]["event_id"]).is_open
    payload = h.mon.hub_status_payload()
    assert payload["pc_health"]["sample"]["memory_percent"] == 40.0 and payload["stalled"] is False
    assert "PC: CPU" in h.mon.command_status()


# ---------------------------------------------------------------- watchdog

def test_stall_detector_reports_once_per_stall():
    clock = FakeClock()
    d = StallDetector(stall_after=120, mono=clock)
    d.beat(); clock.advance(60)
    assert d.check() is None
    clock.advance(70)
    assert d.check() == pytest.approx(130) and d.check() is None and d.stalls == 1
    d.beat(); clock.advance(200)
    assert d.check() == pytest.approx(200) and d.stalls == 2


class FakeProc:
    def __init__(self, rc):
        self._rc, self.polls, self.terminated = rc, 0, False

    def poll(self):
        self.polls += 1
        return self._rc if self.polls >= 2 else None

    def terminate(self):
        self.terminated = True


def test_supervisor_restarts_with_backoff_and_gives_up():
    clock = FakeClock()
    sleeps = []
    def sleep(s): sleeps.append(s); clock.advance(s)
    codes = iter([1, 1, 0])
    events = []
    sup = Supervisor(lambda: FakeProc(next(codes)), SupervisorPolicy(backoff_base=5, backoff_max=40, max_restarts_per_hour=10),
                     mono=clock, sleep=sleep, on_event=events.append)
    st = sup.run()
    assert st.restarts == 2 and not st.gave_up and [s for s in sleeps if s > 1] == [5, 10]
    assert any("exited normally" in e for e in events)
    # crash loop -> gives up after the hourly cap
    sup2 = Supervisor(lambda: FakeProc(3), SupervisorPolicy(backoff_base=1, backoff_max=1, max_restarts_per_hour=3),
                      mono=clock, sleep=sleep, on_event=events.append)
    st2 = sup2.run()
    assert st2.gave_up and st2.restarts == 3 and "giving up" in events[-1]
    # spawn failure is reported, not raised
    sup3 = Supervisor(lambda: (_ for _ in ()).throw(OSError("no exe")), SupervisorPolicy(backoff_base=1, backoff_max=1, max_restarts_per_hour=1),
                      mono=clock, sleep=sleep, on_event=events.append)
    assert sup3.run().last_error.startswith("spawn failed")


# ---------------------------------------------------------------- clips

def test_clip_buffer_and_delivery(tmp_path):
    clock = FakeClock()
    buf = ClipBuffer(ClipsConfig(enabled=True, seconds_before=5, fps=1, max_width=64), mono=clock)
    for i in range(8):
        buf.add(fake_frame(320, 180, color=(i * 20, 0, 0)))
        clock.advance(1)
    assert 4 <= len(buf) <= 6                                   # only the last ~5 s are kept
    path = buf.write(tmp_path / "clip.gif")
    assert path and Path(path).exists() and Image.open(path).n_frames >= 2
    assert ClipBuffer(ClipsConfig(enabled=False), mono=clock).write(tmp_path / "x.gif") is None
    tr = FakeTransport()
    client = TelegramClient(TelegramConfig(), "123456789:AAFakeTokenForTests_abcdefghijklmnop", "42", transport=tr)
    deliver(client, {"text": "alert", "caption": "alert", "created_at": clock.now, "clip_path": path}, "", clock)
    methods = [u.rsplit("/", 1)[1] for u, _d, _h in tr.requests]
    assert methods == ["sendMessage", "sendAnimation"]
    tr2 = FakeTransport(responses=[(200, {"ok": True, "result": {"message_id": 5}}), (400, {"ok": False, "description": "Bad Request: file too big"})])
    client2 = TelegramClient(TelegramConfig(), "123456789:AAFakeTokenForTests_abcdefghijklmnop", "42", transport=tr2)
    assert deliver(client2, {"text": "alert", "clip_path": path}, "", clock)["message_id"] == 5     # clip failure never fails the alert


def test_incident_alert_attaches_clip_when_enabled(cfg, rules, clock):
    cfg.clips.enabled = True
    cfg.clips.seconds_before = 20
    h = Harness(cfg, rules, clock)
    h.mon.clips = ClipBuffer(ClipsConfig(enabled=True, seconds_before=20, fps=1, max_width=64), mono=clock)
    h.run(10)
    h.ocr.default = RESTRICTION
    h.run(6)
    alert = [d for d in all_deliveries(h.queue) if d["kind"] == "incident"][0]
    assert alert["payload"]["clip_path"].endswith(".gif") and Path(alert["payload"]["clip_path"]).exists()
    assert "redacted frames" in alert["payload"]["clip_caption"]


# ---------------------------------------------------------------- engagement

def test_engagement_parsing_and_stats():
    assert parse_engagement("LIVE 00:12:34  1,204 viewers  End LIVE") == {"viewers": 1204}
    assert parse_engagement("12.5K viewers · 3.2k likes") == {"viewers": 12500, "likes": 3200}
    assert parse_engagement("2M watching") == {"viewers": 2_000_000}
    assert parse_engagement("Go LIVE  Preview") == {}
    st = EngagementStats(episode_id="EP")
    st.observe(1.0, {"viewers": 100}); st.observe(2.0, {"viewers": 300, "likes": 10}); st.observe(3.0, {"likes": 50})
    assert st.viewers_peak == 300 and st.viewers_avg == 200 and st.likes_peak == 50 and st.samples == 2
    assert "viewers now 300, peak 300, avg 200; likes 50" == st.summary()


def test_engagement_feeds_status_and_report(cfg, rules, clock):
    cfg.activity.session_reports = True
    h = Harness(cfg, rules, clock)
    h.ocr.default = LIVE_TEXT                 # contains "1,204 viewers"
    h.run(30)
    assert h.mon.engagement.viewers_latest == 1204 and h.mon.engagement.episode_id == h.mon.episodes.state.episode_id
    assert "Engagement (Studio counters): viewers now 1204" in h.mon.command_status()
    h.ocr.default = NOT_LIVE_TEXT
    h.run(60)
    report = [d for d in all_deliveries(h.queue) if d["event_id"].startswith("RPT-")][0]
    assert "Engagement (Studio counters)" in report["payload"]["text"] and "peak 1204" in report["payload"]["text"]
    assert h.mon.last_report["engagement"]["viewers_peak"] == 1204


# ---------------------------------------------------------------- hardening

def test_sanitize_redacts_all_secret_shapes():
    s = sanitize("token 123456789:AAFakeTokenForTests_abcdefghijklmnop key sm_abcdefghijklmnop0123 "
                 "Authorization: Bearer 6f1b9b3e-6d55-4a0e-9d2b-0a1b2c3d4e5f:Q29tcGxldGVseVNlY3JldFZhbHVl")
    assert "AAFake" not in s and "sm_abc" not in s and "Q29tcGxl" not in s and s.count("[REDACTED]") == 3


def test_doctor_runs_without_network(cfg, tmp_path):
    from studio_monitor.app import doctor
    rows = doctor(cfg, tmp_path / "cfg.json")
    names = {r[0] for r in rows}
    assert {"OCR backends", "face model", "disk space", "target", "bots", "live-state rules", "hub"} <= names
    assert all(r[1] in ("OK", "WARN", "FAIL") for r in rows)
