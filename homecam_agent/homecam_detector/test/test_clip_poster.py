"""Contract and privacy tests for event clip delivery."""

import json

from homecam_detector.clip_poster import EventClipPoster, event_clip_to_payload
from homecam_detector.event_segmenter import EventClipBoundary


VALID_TOKEN = "hc1.123e4567-e89b-42d3-a456-426614174000." + "a" * 64


def _boundary(phase: str = "started") -> EventClipBoundary:
    ended = phase == "ended"
    return EventClipBoundary(
        phase=phase,
        event_group_id="018f1e90-7b80-7000-8000-000000000001",
        segment_index=0,
        primary_type="person",
        labels=("person", "motion"),
        confidence=0.91,
        detected_at=5.0,
        start_at=0.0,
        end_at=12.5 if ended else None,
        monotonic_duration_ms=12_500 if ended else None,
        boot_id="11111111-1111-4111-8111-111111111111",
        session_ids=("22222222-2222-4222-8222-222222222222",),
        clock_stepped_during_event=False,
        notification_eligible=True,
        idempotency_key="a" * 64,
    )


def test_started_payload_contains_session_ids_but_never_stream_arn() -> None:
    payload = event_clip_to_payload(_boundary())
    assert payload["detectedAt"] == "1970-01-01T00:00:05.000Z"
    assert payload["sessionIds"] == [
        "22222222-2222-4222-8222-222222222222"
    ]
    assert "streamArn" not in payload
    assert "endAt" not in payload


def test_ended_payload_is_authoritative() -> None:
    payload = event_clip_to_payload(_boundary("ended"))
    assert payload["endAt"] == "1970-01-01T00:00:12.500Z"
    assert payload["monotonicDurationMs"] == 12_500


def test_privacy_disable_discards_pending_clip_operations() -> None:
    poster = EventClipPoster(
        "https://homecam.example.test", VALID_TOKEN, enabled=False
    )
    event = _boundary()
    assert not poster.enqueue(event)
    poster._queue.put_nowait((poster._generation, event))
    poster.set_enabled(False)
    poster._queue.join()
    assert poster._queue.empty()
    poster.close()


def test_ended_uses_dedicated_endpoint_and_idempotency_header() -> None:
    class Response:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class RecordingOpener:
        request = None

        def open(self, request, timeout):
            assert timeout == 2.0
            self.request = request
            return Response()

    poster = EventClipPoster("https://homecam.example.test", VALID_TOKEN)
    opener = RecordingOpener()
    poster._opener = opener
    assert poster.enqueue(_boundary("ended"))
    poster._queue.join()
    assert opener.request.full_url.endswith("/api/device/v1/event-clips/ended")
    assert opener.request.get_header("Idempotency-key") == "a" * 64
    assert json.loads(opener.request.data)["monotonicDurationMs"] == 12_500
    poster.close()
