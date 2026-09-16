"""Consecutive-frame confirmation and cooldown for home-camera events."""

from dataclasses import dataclass
import hashlib
from typing import Dict, Iterable, List, Mapping, Optional


@dataclass(frozen=True)
class ConfirmedEvent:
    """A backend-ready, idempotent event."""

    event_type: str
    confidence: float
    occurred_at: float
    idempotency_key: str


class EventDedupe:
    """Confirm candidates and suppress repeated alerts during a cooldown."""

    def __init__(
        self,
        device_id: str,
        consecutive_frames: int = 3,
        cooldown_sec: float = 30.0,
        max_frame_gap_sec: float = 1.0,
        allowed_types: Iterable[str] = ("motion", "person", "dog", "cat"),
    ) -> None:
        if consecutive_frames < 1:
            raise ValueError("consecutive_frames must be at least 1")
        if cooldown_sec < 0.0:
            raise ValueError("cooldown_sec must be non-negative")
        if max_frame_gap_sec <= 0.0:
            raise ValueError("max_frame_gap_sec must be positive")
        self._device_id = device_id
        self._required = consecutive_frames
        self._cooldown = cooldown_sec
        self._max_frame_gap = max_frame_gap_sec
        self._allowed = set(allowed_types)
        self._counts: Dict[str, int] = {}
        self._last_emitted_monotonic: Dict[str, float] = {}
        self._last_observed_monotonic = None
        self._sequence = 0

    def reset(self) -> None:
        """Forget in-progress candidates but retain cooldown history."""
        self._counts.clear()
        self._last_observed_monotonic = None

    def observe(
        self,
        candidates: Mapping[str, float],
        occurred_at: float,
        observed_at: Optional[float] = None,
    ) -> List[ConfirmedEvent]:
        """Consume one frame of candidates and return newly confirmed events."""
        if observed_at is None:
            observed_at = occurred_at
        if (
            self._last_observed_monotonic is not None
            and (
                observed_at < self._last_observed_monotonic
                or observed_at - self._last_observed_monotonic
                > self._max_frame_gap
            )
        ):
            self._counts.clear()
        self._last_observed_monotonic = observed_at

        present = set(candidates).intersection(self._allowed)
        for event_type in list(self._counts):
            if event_type not in present:
                self._counts[event_type] = 0

        emitted: List[ConfirmedEvent] = []
        for event_type in sorted(present):
            self._counts[event_type] = self._counts.get(event_type, 0) + 1
            if self._counts[event_type] < self._required:
                continue
            last = self._last_emitted_monotonic.get(event_type)
            if last is not None and observed_at - last < self._cooldown:
                continue

            self._sequence += 1
            raw_key = (
                f"{self._device_id}:{event_type}:"
                f"{int(occurred_at * 1000)}:{self._sequence}"
            )
            key = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
            emitted.append(
                ConfirmedEvent(
                    event_type=event_type,
                    confidence=float(candidates[event_type]),
                    occurred_at=occurred_at,
                    idempotency_key=key,
                )
            )
            self._last_emitted_monotonic[event_type] = observed_at
        return emitted
