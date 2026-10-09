import json

import pytest

from studio_monitor.config import TelegramConfig
from studio_monitor.queue import DeliveryError, DeliveryQueue, DeliveryWorker
from studio_monitor.telegram import TelegramClient, encode_multipart, make_sender


def test_queue_persists_across_reopen(tmp_path, clock):
    db = tmp_path / "q.sqlite3"
    q = DeliveryQueue(db, clock=clock)
    q.enqueue("INC-1", {"text": "hello"}, "")
    q.close()
    q2 = DeliveryQueue(db, clock=clock)
    item = q2.next_due()
    assert item is not None and item.incident_id == "INC-1" and item.payload["text"] == "hello"


def test_retry_backoff_and_give_up(tmp_path, clock):
    q = DeliveryQueue(tmp_path / "q.sqlite3", max_attempts=3, backoff_base=2, backoff_max=100, clock=clock)
    q.enqueue("INC-1", {"text": "x"}, "")
    calls = []

    def sender(payload, shot):
        calls.append(payload)
        raise DeliveryError("boom")

    w = DeliveryWorker(q, sender)
    assert w.process_once()            # attempt 1 -> retry in 2s
    assert q.next_due() is None
    assert q.seconds_until_next() == pytest.approx(2.0)
    clock.advance(2)
    assert w.process_once()            # attempt 2 -> retry in 4s
    clock.advance(1)
    assert not w.process_once()        # not due yet
    clock.advance(3)
    assert w.process_once()            # attempt 3 -> failed
    assert q.counts()["failed"] == 1 and len(calls) == 3
    assert q.requeue_failed() == 1 and q.next_due() is not None


def test_retry_after_hint_and_permanent(tmp_path, clock):
    q = DeliveryQueue(tmp_path / "q.sqlite3", backoff_base=1, clock=clock)
    a = q.enqueue("A", {"text": "a"}, "")
    b = q.enqueue("B", {"text": "b"}, "")
    assert q.mark_failed(a, "rate limited", retry_after=30) == "pending"
    assert q.seconds_until_next() == pytest.approx(0.0)  # B is still due now
    assert q.mark_failed(b, "bad token", permanent=True) == "failed"
    assert q.next_due() is None
    clock.advance(30)
    assert q.next_due().id == a


def test_successful_delivery_marks_sent(tmp_path, clock):
    q = DeliveryQueue(tmp_path / "q.sqlite3", clock=clock)
    q.enqueue("INC-9", {"text": "ok"}, "")
    events = []
    w = DeliveryWorker(q, lambda p, s: None, on_event=events.append)
    assert w.process_once()
    assert q.counts() == {"pending": 0, "sent": 1, "failed": 0}
    assert "delivered" in events[0]


# ---- Telegram client ----------------------------------------------------

class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, url, data, headers, timeout):
        self.requests.append((url, data, headers))
        status, body = self.responses.pop(0)
        return status, json.dumps(body).encode()


def _client(responses):
    t = FakeTransport(responses)
    return TelegramClient(TelegramConfig(bot_token="123:SECRET", chat_id="7"), transport=t), t


def test_send_message_ok():
    c, t = _client([(200, {"ok": True, "result": {"message_id": 1}})])
    assert c.send_message("hi")["message_id"] == 1
    url, data, headers = t.requests[0]
    assert url.endswith("/bot123:SECRET/sendMessage") and b"chat_id=7" in data


def test_rate_limit_is_retryable_with_hint():
    c, _ = _client([(429, {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 17}})])
    with pytest.raises(DeliveryError) as e:
        c.send_message("hi")
    assert e.value.retry_after == 17 and not e.value.permanent


def test_server_error_retryable_and_token_masked():
    c, _ = _client([(502, {"ok": False, "description": "Bad Gateway for bot123:SECRET"})])
    with pytest.raises(DeliveryError) as e:
        c.send_message("hi")
    assert "SECRET" not in str(e.value) and not e.value.permanent


def test_unauthorized_is_permanent():
    c, _ = _client([(401, {"ok": False, "description": "Unauthorized"})])
    with pytest.raises(DeliveryError) as e:
        c.send_message("hi")
    assert e.value.permanent


def test_unconfigured_is_permanent():
    c = TelegramClient(TelegramConfig())
    with pytest.raises(DeliveryError) as e:
        c.send_message("hi")
    assert e.value.permanent


def test_send_photo_multipart(tmp_path):
    shot = tmp_path / "s.png"
    shot.write_bytes(b"\x89PNG fake")
    c, t = _client([(200, {"ok": True, "result": {}})])
    sender = make_sender(c)
    sender({"caption": "cap", "text": "txt"}, str(shot))
    url, data, headers = t.requests[0]
    assert url.endswith("/sendPhoto")
    assert headers["Content-Type"].startswith("multipart/form-data")
    assert b'filename="s.png"' in data and b"\x89PNG fake" in data


def test_sender_falls_back_to_text_when_screenshot_missing():
    c, t = _client([(200, {"ok": True, "result": {}})])
    make_sender(c)({"caption": "cap", "text": "txt"}, "/nonexistent/x.png")
    assert t.requests[0][0].endswith("/sendMessage")
    assert b"no+longer+available" in t.requests[0][1]


def test_encode_multipart_shape():
    body, ctype = encode_multipart({"a": "1"}, {"photo": ("x.png", b"data")})
    boundary = ctype.split("boundary=")[1]
    assert body.startswith(f"--{boundary}".encode()) and body.endswith(f"--{boundary}--\r\n".encode())
