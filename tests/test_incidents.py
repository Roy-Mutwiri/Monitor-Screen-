from studio_monitor.incidents import IncidentTracker, fingerprint


def test_fingerprint_ignores_digits_and_case():
    assert fingerprint("a", "Restricted for 3 days") == fingerprint("a", "RESTRICTED for 7 DAYS")


def test_confirmation_requires_consecutive_polls(clock):
    t = IncidentTracker(confirm_polls=2, clock=clock)
    d1 = t.observe("restriction_notice", "Restriction", "your live has been restricted")
    assert not d1.alert and "awaiting" in d1.reason
    clock.advance(2)
    d2 = t.observe("restriction_notice", "Restriction", "your live has been restricted")
    assert d2.alert and d2.incident.incident_id.startswith("INC-")


def test_duplicate_suppressed_until_cooldown(clock):
    t = IncidentTracker(confirm_polls=1, cooldown_seconds=600, resolve_after_seconds=30, clock=clock)
    first = t.observe("content_warning", "Warning", "your live may violate guidelines")
    assert first.alert
    for _ in range(10):
        clock.advance(2)
        assert not t.observe("content_warning", "Warning", "your live may violate guidelines").alert
    clock.advance(600)
    again = t.observe("content_warning", "Warning", "your live may violate guidelines")
    assert again.alert and again.incident.incident_id == first.incident.incident_id
    assert again.incident.alerts_sent == 2


def test_reappearance_after_resolve_gets_new_id(clock):
    t = IncidentTracker(confirm_polls=1, cooldown_seconds=600, resolve_after_seconds=30, clock=clock)
    first = t.observe("live_interruption", "LIVE interruption", "your live has ended")
    first_id = first.incident.incident_id
    clock.advance(45)  # popup gone for > resolve_after
    again = t.observe("live_interruption", "LIVE interruption", "your live has ended")
    assert again.alert and again.incident.incident_id != first_id


def test_fuzzy_match_counts_as_duplicate(clock):
    t = IncidentTracker(confirm_polls=1, clock=clock)
    assert t.observe("restriction_notice", "R", "Your LIVE has been restricted until Oct 12, 2026 10:00").alert
    clock.advance(2)
    d = t.observe("restriction_notice", "R", "Your LIVE has been restricted until 0ct 12. 2026 10:01")
    assert not d.alert and d.reason == "duplicate of active incident"


def test_pending_expires(clock):
    t = IncidentTracker(confirm_polls=3, resolve_after_seconds=10, clock=clock)
    t.observe("x", "X", "some popup text here")
    clock.advance(60)
    t.tick()
    d = t.observe("x", "X", "some popup text here")
    assert "1/3" in d.reason
