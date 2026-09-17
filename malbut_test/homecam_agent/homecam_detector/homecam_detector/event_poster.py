"""Bounded, retrying HTTPS event delivery outside the ROS image callback."""

from datetime import datetime, timezone
import json
import queue
import threading
import time
from typing import Callable, Dict, Optional, Tuple, Union
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPRedirectHandler,
    OpenerDirector,
    Request,
    build_opener,
)

from .credentials import is_valid_device_token
from .event_dedupe import ConfirmedEvent


class _NoRedirectHandler(HTTPRedirectHandler):
    """Turn every HTTP redirect into an HTTPError without forwarding headers."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Refuse creation of a redirected request."""
        del req, fp, code, msg, headers, newurl
        return None


def build_no_redirect_opener() -> OpenerDirector:
    """Build the HTTPS opener used for credential-bearing device requests."""
    return build_opener(_NoRedirectHandler())


def event_to_payload(
    event: ConfirmedEvent,
) -> Dict[str, Union[str, float]]:
    """Convert an event to the backend's strict camelCase contract."""
    return {
        "eventType": event.event_type,
        "confidence": event.confidence,
        "occurredAt": datetime.fromtimestamp(event.occurred_at, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "idempotencyKey": event.idempotency_key,
    }


class EventPoster:
    """Post idempotent events from one bounded worker thread."""

    def __init__(
        self,
        backend_url: str,
        bearer_token: str,
        enabled: bool = True,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        if not is_valid_device_token(bearer_token):
            raise ValueError("invalid homecam device credential")
        self._url = backend_url.rstrip("/") + "/api/device/v1/events"
        self._token = bearer_token
        self._on_error = on_error or (lambda _message: None)
        self._opener = build_no_redirect_opener()
        self._queue: "queue.Queue[Tuple[int, ConfirmedEvent]]" = queue.Queue(
            maxsize=100
        )
        self._stop_requested = threading.Event()
        self._delivery_enabled = threading.Event()
        self._generation = 0
        if enabled:
            self._delivery_enabled.set()
        self._state_lock = threading.Lock()
        self._worker = threading.Thread(
            target=self._run, name="homecam-event-poster", daemon=True
        )
        self._worker.start()

    def enqueue(self, event: ConfirmedEvent) -> bool:
        """Queue an event without blocking the camera callback."""
        with self._state_lock:
            if (
                self._stop_requested.is_set()
                or not self._delivery_enabled.is_set()
            ):
                return False
            try:
                self._queue.put_nowait((self._generation, event))
                return True
            except queue.Full:
                self._on_error("event queue is full; dropping newest event")
                return False

    def set_enabled(self, enabled: bool) -> None:
        """Enable delivery or atomically disable it and discard queued events."""
        with self._state_lock:
            if enabled and not self._stop_requested.is_set():
                self._delivery_enabled.set()
                return
            self._generation += 1
            self._delivery_enabled.clear()
            self._discard_pending()

    def close(self) -> None:
        """Stop accepting work and synchronously join the delivery thread."""
        with self._state_lock:
            self._stop_requested.set()
            self._generation += 1
            self._delivery_enabled.clear()
            self._discard_pending()
        self._worker.join(timeout=3.0)
        if self._worker.is_alive():
            self._on_error("event worker did not stop during shutdown")

    def _run(self) -> None:
        while not self._stop_requested.is_set():
            try:
                generation, event = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if not self._is_current(generation):
                    continue
                try:
                    self._post_with_retry(generation, event)
                except Exception as error:  # Keep the sole worker alive.
                    self._on_error(
                        f"unexpected event delivery failure: {type(error).__name__}"
                    )
            finally:
                self._queue.task_done()

    def _post_with_retry(
        self, generation: int, event: ConfirmedEvent
    ) -> None:
        payload = event_to_payload(event)
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = Request(
            self._url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Idempotency-Key": event.idempotency_key,
            },
        )
        for attempt in range(3):
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
                if attempt == 2:
                    self._on_error(
                        f"event {event.idempotency_key[:8]} delivery failed: {error}"
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
