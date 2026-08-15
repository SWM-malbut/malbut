"""Privacy-aware delivery for event clip started/ended operations."""

from datetime import datetime, timezone
import json
import queue
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple, Union
from urllib.error import HTTPError, URLError
from urllib.request import Request

from .credentials import is_valid_device_token
from .event_poster import build_no_redirect_opener
from .event_segmenter import EventClipBoundary


JsonValue = Union[str, float, int, bool, List[str], None]


def _utc_timestamp(value: float) -> str:
    return (
        datetime.fromtimestamp(value, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def event_clip_to_payload(event: EventClipBoundary) -> Dict[str, JsonValue]:
    """Serialize a clip boundary without exposing AWS credentials or ARNs."""
    payload: Dict[str, JsonValue] = {
        "eventGroupId": event.event_group_id,
        "segmentIndex": event.segment_index,
        "primaryType": event.primary_type,
        "labels": list(event.labels),
        "confidence": event.confidence,
        "detectedAt": _utc_timestamp(event.detected_at),
        "startAt": _utc_timestamp(event.start_at),
        "bootId": event.boot_id,
        "sessionIds": list(event.session_ids),
        "clockSource": "wall",
        "clockSteppedDuringEvent": event.clock_stepped_during_event,
        "notificationEligible": event.notification_eligible,
        "idempotencyKey": event.idempotency_key,
    }
    if event.phase == "ended":
        if event.end_at is None or event.monotonic_duration_ms is None:
            raise ValueError("ended event clips require end time and duration")
        payload["endAt"] = _utc_timestamp(event.end_at)
        payload["monotonicDurationMs"] = event.monotonic_duration_ms
    elif event.phase != "started":
        raise ValueError("event clip phase must be started or ended")
    return payload


class EventClipPoster:
    """Post bounded clip operations from one non-blocking worker thread."""

    def __init__(
        self,
        backend_url: str,
        bearer_token: str,
        *,
        enabled: bool = True,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        if not is_valid_device_token(bearer_token):
            raise ValueError("invalid homecam device credential")
        self._base_url = backend_url.rstrip("/") + "/api/device/v1/event-clips"
        self._token = bearer_token
        self._on_error = on_error or (lambda _message: None)
        self._opener = build_no_redirect_opener()
        self._queue: "queue.Queue[Tuple[int, EventClipBoundary]]" = queue.Queue(
            maxsize=100
        )
        self._stop_requested = threading.Event()
        self._delivery_enabled = threading.Event()
        self._generation = 0
        if enabled:
            self._delivery_enabled.set()
        self._state_lock = threading.Lock()
        self._worker = threading.Thread(
            target=self._run, name="homecam-event-clip-poster", daemon=True
        )
        self._worker.start()

    def enqueue(self, event: EventClipBoundary) -> bool:
        with self._state_lock:
            if self._stop_requested.is_set() or not self._delivery_enabled.is_set():
                return False
            try:
                self._queue.put_nowait((self._generation, event))
                return True
            except queue.Full:
                self._on_error("event clip queue is full; dropping newest operation")
                return False

    def set_enabled(self, enabled: bool) -> None:
        """Disable atomically and discard pending privacy-sensitive work."""
        with self._state_lock:
            if enabled and not self._stop_requested.is_set():
                self._delivery_enabled.set()
                return
            self._generation += 1
            self._delivery_enabled.clear()
            self._discard_pending()

    def close(self) -> None:
        with self._state_lock:
            self._stop_requested.set()
            self._generation += 1
            self._delivery_enabled.clear()
            self._discard_pending()
        self._worker.join(timeout=3.0)
        if self._worker.is_alive():
            self._on_error("event clip worker did not stop during shutdown")

    def _run(self) -> None:
        while not self._stop_requested.is_set():
            try:
                generation, event = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if self._is_current(generation):
                    self._post_with_retry(generation, event)
            except Exception as error:  # Keep the sole worker alive.
                self._on_error(
                    "unexpected event clip delivery failure: "
                    f"{type(error).__name__}"
                )
            finally:
                self._queue.task_done()

    def _post_with_retry(
        self, generation: int, event: EventClipBoundary
    ) -> None:
        request = Request(
            f"{self._base_url}/{event.phase}",
            data=json.dumps(
                event_clip_to_payload(event), separators=(",", ":")
            ).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Idempotency-Key": event.idempotency_key,
            },
        )
        attempts = 2 if event.phase == "started" else 3
        for attempt in range(attempts):
            if not self._is_current(generation):
                return
            try:
                with self._opener.open(request, timeout=2.0) as response:
                    if 200 <= response.status < 300:
                        return
                    raise RuntimeError(f"backend returned HTTP {response.status}")
            except (HTTPError, URLError, TimeoutError, RuntimeError) as error:
                if not self._is_current(generation):
                    return
                if attempt + 1 == attempts:
                    self._on_error(
                        f"event clip {event.idempotency_key[:8]} "
                        f"delivery failed: {error}"
                    )
                    return
                time.sleep(0.25 * (2**attempt))

    def _is_current(self, generation: int) -> bool:
        with self._state_lock:
            return (
                not self._stop_requested.is_set()
                and self._delivery_enabled.is_set()
                and generation == self._generation
            )

    def _discard_pending(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return
            self._queue.task_done()
