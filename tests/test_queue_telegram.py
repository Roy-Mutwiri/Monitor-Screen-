"""Outbox (events + per-bot deliveries), worker, Telegram client."""
import json

import pytest

from conftest import TOKEN_A, TOKEN_B, FakeTransport, all_deliveries

from studio_monitor.bots import BotTarget
from studio_monitor.config import TelegramConfig
from studio_monitor.queue import DeliveryError, DeliveryQueue, DeliveryWorker
from studio_monitor.telegram import ClientFactory, TelegramClient, deliver, encode_multipart, sanitize

T1 = BotTarget("b1", "One", "11", None)
T2 = BotTarget("b2", "Two", "-100222", 7)


def q_(tmp_path, clock, **kw):
    return DeliveryQueue(tmp_path / "q.sqlite3", clock=clock, sanitizer=lambda s: sanitize(s), **kw)


def test_event_fans_out_to_each_bot_with_destination_snapshot(tmp_path, clock):
    q = q_(tmp_path, clock)
    n = q.create_event("E1", "incident", "restrictions", {"text": "hello"}, "/shots/E1.png", [T1, T2], label="L")
    assert n == 2
    ds = q.deliveries_for("E1")
    assert [(d.bot_id, d.chat_id, d.thread_id) for d in ds] == [("b1", "11", None), ("b2", "-100222", 7)]
    assert all(d.evidence_path == "/shots/E1.png" for d in ds)      # shared evidence, one file
    assert q.create_event("E1", "incident", "restrictions", {"text": "x"}, "", [T1, T2]) == 0  # unique (event, bot)
    assert q.counts()["pending"] == 2


def test_persists_across_reopen(tmp_path, clock):
    q = q_(tmp_path, clock)
    q.create_event("E1", "incident", "restrictions", {"text": "hello"}, "", [T1])
    q.close()
    q2 = DeliveryQueue(tmp_path / "q.sqlite3", clock=clock)
    d = q2.due_deliveries()[0]
    assert d.event_id == "E1" and d.payload["text"] == "hello" and d.bot_id == "b1"


def test_retry_backoff_and_give_up_per_delivery(tmp_path, clock):
    q = q_(tmp_path, clock, max_attempts=3, backoff_base=2, backoff_max=100)
    q.create_event("E1", "incident", "restrictions", {"text": "x"}, "", [T1])
    calls = []

    def send(d):
        calls.append(d.id)
        raise DeliveryError("boom")

    w = DeliveryWorker(q, send)
    assert w.process_round() == 1            # attempt 1 -> retry in 2s
    assert q.due_deliveries() == [] and q.seconds_until_next() == pytest.approx(2.0)
    clock.advance(2)
    assert w.process_round() == 1            # attempt 2 -> retry in 4s
    clock.advance(1)
    assert w.process_round() == 0            # not due yet
    clock.advance(3)
    assert w.process_round() == 1            # attempt 3 -> failed
    assert q.counts()["failed"] == 1 and len(calls) == 3
    assert q.requeue_failed() == 1 and q.due_deliveries()


def test_failing_bot_does_not_block_others(tmp_path, clock):
    q = q_(tmp_path, clock)
    q.create_event("E1", "incident", "restrictions", {"text": "x"}, "", [T1, T2])
    sent = []

    def send(d):
        if d.bot_id == "b1":
            raise DeliveryError("token rejected", permanent=True)
        sent.append(d.bot_id)
        return 99

    DeliveryWorker(q, send).process_round()
    ds = {d.bot_id: d for d in q.deliveries_for("E1")}
    assert ds["b1"].status == "failed" and ds["b2"].status == "sent" and ds["b2"].message_id == 99
    assert q.event_summary("E1")["text"] == "Delivered to 1 of 2 bots — 1 blocked"


def test_rate_limit_blocks_only_that_bot(tmp_path, clock):
    q = q_(tmp_path, clock, backoff_base=1)
    q.create_event("E1", "incident", "restrictions", {"text": "x"}, "", [T1, T2])
    q.create_event("E2", "incident", "restrictions", {"text": "y"}, "", [T1, T2])

    def send(d):
        if d.bot_id == "b1":
            raise DeliveryError("rate limited", retry_after=30)
        return 1

    w = DeliveryWorker(q, send)
    w.process_round()                         # E1: b1 limited for 30s, b2 sent
    due = q.due_deliveries()
    assert [d.bot_id for d in due] == ["b2"]  # b1's E2 waits (bot blocked), b2's E2 is due
    w.process_round()
    assert [d.status for d in q.deliveries_for("E2") if d.bot_id == "b2"] == ["sent"]
    assert q.bot_stats("b1")["blocked_until"] == pytest.approx(clock() + 30)
    clock.advance(31)
    assert [d.bot_id for d in q.due_deliveries()] == ["b1"]


def test_one_in_flight_per_bot_and_oldest_first(tmp_path, clock):
    q = q_(tmp_path, clock)
    q.create_event("E1", "incident", "restrictions", {"text": "x"}, "", [T1])
    q.create_event("E2", "incident", "restrictions", {"text": "y"}, "", [T1])
    due = q.due_deliveries()
    assert len(due) == 1 and due[0].event_id == "E1"
    assert q.due_deliveries(exclude_bots={"b1"}) == []


def test_cancel_retry_expire_and_evidence_in_use(tmp_path, clock):
    q = q_(tmp_path, clock)
    q.create_event("E1", "incident", "restrictions", {"text": "x"}, "/e/E1.png", [T1, T2])
    assert q.evidence_in_use() == {"/e/E1.png"}
    assert q.cancel_bot_pending("b1", "bot disabled") == 1
    d2 = [d for d in q.deliveries_for("E1") if d.bot_id == "b2"][0]
    q.mark_failed(d2.id, "token rejected", permanent=True)
    assert q.evidence_in_use() == set()
    assert q.retry_delivery(d2.id) and q.delivery(d2.id).status == "pending"
    d1 = [d for d in q.deliveries_for("E1") if d.bot_id == "b1"][0]
    q.mark_sent(d2.id, 5)
    assert not q.retry_delivery(d2.id)         # never re-send a success
    assert q.retry_delivery(d1.id)
    clock.advance(49 * 3600)
    assert q.expire_stale(48 * 3600) == 1 and q.delivery(d1.id).status == "dead"
    assert "expired" in q.delivery(d1.id).last_error
    assert q.evidence_in_use() == set()


def test_transaction_rollback_and_kv(tmp_path, clock):
    q = q_(tmp_path, clock)
    with pytest.raises(RuntimeError):
        with q.transaction() as conn:
            q.create_event("E9", "incident", "restrictions", {}, "", [T1], conn=conn)
            raise RuntimeError("boom")
    assert q.counts()["pending"] == 0
    q.set_state("k", {"a": 1})
    assert q.get_state("k") == {"a": 1} and q.get_state("missing", 5) == 5


def test_errors_are_sanitized_in_db(tmp_path, clock):
    q = q_(tmp_path, clock)
    q.create_event("E1", "incident", "restrictions", {}, "", [T1])
    d = q.due_deliveries()[0]
    q.mark_failed(d.id, f"HTTP 500 at https://api.telegram.org/bot{TOKEN_A}/sendPhoto")
    err = q.delivery(d.id).last_error
    assert TOKEN_A not in err and "[REDACTED]" in err
    assert TOKEN_A not in q.bot_stats("b1")["last_result"]


def test_history_and_summary(tmp_path, clock):
    q = q_(tmp_path, clock)
    q.create_event("E1", "incident", "restrictions", {}, "", [T1, T2], label="Restriction")
    clock.advance(1)
    q.create_event("E2", "activity", "studio_opened", {}, "", [T1], label="Studio opened")
    h = q.history(10, "all")
    assert [x["id"] for x in h] == ["E2", "E1"]
    assert q.history(10, "incident")[0]["id"] == "E1" and q.history(10, "activity")[0]["id"] == "E2"
    assert h[1]["detail"] == "Delivered to 0 of 2 bots — 2 retrying"
    assert q.summarize([])["text"] == "no bots subscribed"


# ---- Telegram client ----------------------------------------------------

def _client(responses, token=TOKEN_A, chat="7", thread=None):
    t = FakeTransport(responses)
    return TelegramClient(TelegramConfig(), token, chat, thread, transport=t), t


def test_send_message_ok_with_thread():
    c, t = _client([(200, {"ok": True, "result": {"message_id": 1}})], thread=55)
    assert c.send_message("hi")["message_id"] == 1
    url, data, headers = t.requests[0]
    assert url.endswith(f"/bot{TOKEN_A}/sendMessage") and b"chat_id=7" in data and b"message_thread_id=55" in data


def test_rate_limit_is_retryable_with_hint():
    c, _ = _client([(429, {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 17}})])
    with pytest.raises(DeliveryError) as e:
        c.send_message("hi")
    assert e.value.retry_after == 17 and not e.value.permanent


def test_server_error_retryable_and_token_redacted():
    c, _ = _client([(502, {"ok": False, "description": f"Bad Gateway for bot{TOKEN_A}"})])
    with pytest.raises(DeliveryError) as e:
        c.send_message("hi")
    assert TOKEN_A not in str(e.value) and "[REDACTED]" in str(e.value) and not e.value.permanent


@pytest.mark.parametrize("status,desc,permanent,needle", [
    (401, "Unauthorized", True, "token rejected"),
    (403, "Forbidden: bot was blocked by the user", True, "destination refused"),
    (400, "Bad Request: chat not found", True, "destination problem (token is fine)"),
    (400, "Bad Request: message is too long", False, "rejected request"),
])
def test_error_classification(status, desc, permanent, needle):
    c, _ = _client([(status, {"ok": False, "description": desc})])
    with pytest.raises(DeliveryError) as e:
        c.send_message("hi")
    assert e.value.permanent is permanent and needle in str(e.value)


def test_network_exception_text_is_sanitized():
    def transport(url, data, headers, timeout):
        raise OSError(f"connection refused to {url}")
    c = TelegramClient(TelegramConfig(), TOKEN_A, "7", transport=transport)
    with pytest.raises(DeliveryError) as e:
        c.get_me()
    assert TOKEN_A not in str(e.value)


def test_get_me_sends_nothing_to_a_chat():
    c, t = _client([(200, {"ok": True, "result": {"id": 1, "username": "x_bot"}})])
    assert c.get_me()["username"] == "x_bot"
    assert t.requests[0][0].endswith("/getMe") and b"chat_id" not in (t.requests[0][1] or b"")


def test_deliver_photo_multipart_and_text_fallback(tmp_path):
    shot = tmp_path / "s.png"
    shot.write_bytes(b"\x89PNG fake")
    c, t = _client([(200, {"ok": True, "result": {"message_id": 3}})])
    assert deliver(c, {"caption": "cap", "text": "txt", "created_at": 0}, str(shot), clock=lambda: 10)["message_id"] == 3
    url, data, headers = t.requests[0]
    assert url.endswith("/sendPhoto") and headers["Content-Type"].startswith("multipart/form-data")
    assert b'filename="s.png"' in data and b"\x89PNG fake" in data
    c2, t2 = _client([(200, {"ok": True, "result": {}})])
    deliver(c2, {"caption": "cap", "text": "txt"}, "/nonexistent/x.png")
    assert t2.requests[0][0].endswith("/sendMessage") and b"no+longer+available" in t2.requests[0][1]


def test_late_delivery_note_added():
    import time
    c, t = _client([(200, {"ok": True, "result": {}})])
    deliver(c, {"text": "old", "created_at": time.time() - 3600}, "")
    assert b"Delayed+delivery" in t.requests[0][1]


def test_client_factory_caches_and_invalidates():
    tokens = {"b1": TOKEN_A}
    f = ClientFactory(TelegramConfig(), lambda bid: tokens.get(bid), transport=FakeTransport())
    assert f.client("b1", "1", None).token == TOKEN_A
    tokens["b1"] = TOKEN_B
    assert f.client("b1", "1", None).token == TOKEN_A      # cached
    f.invalidate("b1")
    assert f.client("b1", "1", None).token == TOKEN_B
    assert f.client("missing", "1", None) is None


def test_sanitize_and_multipart():
    s = sanitize(f"GET https://api.telegram.org/bot{TOKEN_A}/sendPhoto and bare {TOKEN_B}")
    assert TOKEN_A not in s and TOKEN_B not in s and s.count("[REDACTED]") == 2
    body, ctype = encode_multipart({"a": "1"}, {"photo": ("x.png", b"data")})
    boundary = ctype.split("boundary=")[1]
    assert body.startswith(f"--{boundary}".encode()) and body.endswith(f"--{boundary}--\r\n".encode())
