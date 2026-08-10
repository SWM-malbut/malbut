"""Tests for consecutive confirmation, cooldown, and idempotency."""

from homecam_detector.event_dedupe import EventDedupe
from homecam_detector.event_poster import event_to_payload


def test_requires_consecutive_frames() -> None:
    dedupe = EventDedupe("device-1", consecutive_frames=3, cooldown_sec=30.0)
    assert dedupe.observe({"person": 0.8}, 100.0) == []
    assert dedupe.observe({"person": 0.9}, 100.1) == []
    events = dedupe.observe({"person": 0.95}, 100.2)
    assert len(events) == 1
    assert events[0].event_type == "person"
    assert events[0].confidence == 0.95
    assert len(events[0].idempotency_key) == 64


def test_missing_frame_resets_confirmation() -> None:
    dedupe = EventDedupe("device-1", consecutive_frames=2, cooldown_sec=0.0)
    assert dedupe.observe({"cat": 0.8}, 1.0) == []
    assert dedupe.observe({}, 1.1) == []
    assert dedupe.observe({"cat": 0.8}, 1.2) == []
    assert len(dedupe.observe({"cat": 0.8}, 1.3)) == 1


def test_cooldown_suppresses_duplicate_but_allows_other_class() -> None:
    dedupe = EventDedupe("device-1", consecutive_frames=1, cooldown_sec=10.0)
    first = dedupe.observe({"dog": 0.8}, 20.0)
    assert len(first) == 1
    assert dedupe.observe({"dog": 0.9}, 25.0) == []
    cat = dedupe.observe({"dog": 0.9, "cat": 0.7}, 25.1)
    assert [event.event_type for event in cat] == ["cat"]
    later = dedupe.observe({"dog": 0.95}, 30.0)
    assert [event.event_type for event in later] == ["dog"]
    assert later[0].idempotency_key != first[0].idempotency_key


def test_backend_payload_uses_strict_camel_case_and_utc() -> None:
    dedupe = EventDedupe("device-1", consecutive_frames=1, cooldown_sec=0.0)
    event = dedupe.observe({"person": 0.91}, 0.0)[0]
    payload = event_to_payload(event)
    assert set(payload) == {
        "eventType",
        "confidence",
        "occurredAt",
        "idempotencyKey",
    }
    assert payload["eventType"] == "person"
    assert payload["occurredAt"] == "1970-01-01T00:00:00.000Z"


def test_wall_clock_jumps_do_not_change_monotonic_cooldown() -> None:
    dedupe = EventDedupe("device-1", consecutive_frames=1, cooldown_sec=10.0)
    assert dedupe.observe({"person": 0.9}, 1000.0, observed_at=10.0)
    # Large forward wall jump does not bypass the monotonic cooldown.
    assert dedupe.observe({"person": 0.9}, 999999.0, observed_at=15.0) == []
    # Backward wall jump does not prolong it once monotonic time reaches 10 s.
    assert dedupe.observe({"person": 0.9}, 1.0, observed_at=20.0)


def test_long_camera_gap_resets_consecutive_confirmation() -> None:
    dedupe = EventDedupe(
        "device-1",
        consecutive_frames=2,
        cooldown_sec=0.0,
        max_frame_gap_sec=0.5,
    )
    assert dedupe.observe({"cat": 0.8}, 1.0, observed_at=1.0) == []
    # This cannot complete the earlier candidate because the frame gap is long.
    assert dedupe.observe({"cat": 0.8}, 2.0, observed_at=2.0) == []
    assert dedupe.observe({"cat": 0.8}, 2.1, observed_at=2.1)
