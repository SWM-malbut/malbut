"""Turn detector observations into bounded, idempotent event-clip segments."""

from collections import deque
from dataclasses import dataclass
import hashlib
import math
import secrets
from typing import Callable, Deque, Dict, Iterable, List, Mapping, Optional, Tuple
import uuid


_TYPE_PRIORITY = {
    "motion": 0,
    "cat": 1,
    "dog": 1,
    "person": 2,
    "fall": 3,
}


def uuid7(timestamp_ms: int) -> str:
    """Return an RFC 9562 UUIDv7 on Python versions without uuid.uuid7()."""
    if not 0 <= timestamp_ms < 1 << 48:
        raise ValueError("timestamp_ms is outside the UUIDv7 48-bit range")
    value = (
        (timestamp_ms << 80)
        | (0x7 << 76)
        | (secrets.randbits(12) << 64)
        | (0b10 << 62)
        | secrets.randbits(62)
    )
    return str(uuid.UUID(int=value))


@dataclass(frozen=True)
class EventClipBoundary:
    """One backend-ready started or ended event-clip operation."""

    phase: str
    event_group_id: str
    segment_index: int
    primary_type: str
    labels: Tuple[str, ...]
    confidence: float
    detected_at: float
    start_at: float
    end_at: Optional[float]
    monotonic_duration_ms: Optional[int]
    boot_id: str
    session_ids: Tuple[str, ...]
    clock_stepped_during_event: bool
    notification_eligible: bool
    idempotency_key: str


@dataclass
class _ActiveSegment:
    event_group_id: str
    segment_index: int
    detected_at_wall: float
    segment_start_wall: float
    segment_start_monotonic: float
    last_seen_monotonic: float
    confidence_by_type: Dict[str, float]
    session_ids: set[str]
    notification_eligible: bool
    clock_stepped: bool = False


class EventSegmenter:
    """Confirm M-of-N candidates and merge them into bounded clip segments."""

    def __init__(
        self,
        device_id: str,
        *,
        confirmation_window_frames: int = 5,
        confirmation_required_frames: int = 3,
        pre_roll_sec: float = 5.0,
        merge_gap_sec: float = 10.0,
        max_segment_sec: float = 120.0,
        notification_cooldown_sec: float = 30.0,
        max_frame_gap_sec: float = 1.0,
        allowed_types: Iterable[str] = ("motion", "person", "dog", "cat"),
        boot_id: Optional[str] = None,
        group_id_factory: Optional[Callable[[int], str]] = None,
    ) -> None:
        if not device_id:
            raise ValueError("device_id must not be empty")
        if confirmation_window_frames < 1:
            raise ValueError("confirmation_window_frames must be at least 1")
        if not 1 <= confirmation_required_frames <= confirmation_window_frames:
            raise ValueError("confirmation_required_frames must be within the window")
        for name, value in (
            ("pre_roll_sec", pre_roll_sec),
            ("merge_gap_sec", merge_gap_sec),
            ("max_segment_sec", max_segment_sec),
            ("notification_cooldown_sec", notification_cooldown_sec),
            ("max_frame_gap_sec", max_frame_gap_sec),
        ):
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and non-negative")
        if max_segment_sec <= 0.0:
            raise ValueError("max_segment_sec must be positive")
        if max_frame_gap_sec <= 0.0:
            raise ValueError("max_frame_gap_sec must be positive")
        if max_segment_sec <= pre_roll_sec:
            raise ValueError("max_segment_sec must be greater than pre_roll_sec")

        self._device_id = device_id
        self._window_size = confirmation_window_frames
        self._required = confirmation_required_frames
        self._pre_roll = pre_roll_sec
        self._merge_gap = merge_gap_sec
        self._max_segment = max_segment_sec
        self._notification_cooldown = notification_cooldown_sec
        self._max_frame_gap = max_frame_gap_sec
        self._allowed = set(allowed_types)
        if not self._allowed:
            raise ValueError("allowed_types must not be empty")
        self._history: Dict[str, Deque[Optional[float]]] = {
            event_type: deque(maxlen=self._window_size)
            for event_type in self._allowed
        }
        self._boot_id = boot_id or str(uuid.uuid4())
        self._group_id_factory = group_id_factory or uuid7
        self._active: Optional[_ActiveSegment] = None
        self._last_observed_monotonic: Optional[float] = None
        self._last_observed_wall: Optional[float] = None
        self._last_notification_monotonic: Dict[str, float] = {}

    @property
    def active(self) -> bool:
        return self._active is not None

    def discard(self) -> None:
        """Forget active/candidate state without sending privacy metadata."""
        self._active = None
        self._clear_history()
        self._last_observed_monotonic = None
        self._last_observed_wall = None

    def observe(
        self,
        candidates: Mapping[str, float],
        *,
        occurred_at: float,
        observed_at: float,
        session_id: str,
    ) -> List[EventClipBoundary]:
        """Consume one detector frame and return started/ended operations."""
        if not session_id:
            raise ValueError("session_id must not be empty")
        if not math.isfinite(occurred_at) or not math.isfinite(observed_at):
            raise ValueError("event clocks must be finite")
        if (
            self._last_observed_monotonic is not None
            and observed_at < self._last_observed_monotonic
        ):
            raise ValueError("observed_at must be monotonic")
        if (
            self._last_observed_monotonic is not None
            and observed_at - self._last_observed_monotonic > self._max_frame_gap
        ):
            self._clear_history()

        if (
            self._active is not None
            and self._last_observed_monotonic is not None
            and self._last_observed_wall is not None
        ):
            monotonic_delta = observed_at - self._last_observed_monotonic
            wall_delta = occurred_at - self._last_observed_wall
            if abs(wall_delta - monotonic_delta) > 2.0:
                self._active.clock_stepped = True
            self._active.session_ids.add(session_id)

        self._last_observed_monotonic = observed_at
        self._last_observed_wall = occurred_at
        confirmed = self._confirmed_candidates(candidates)
        emitted: List[EventClipBoundary] = []
        if self._active is not None and confirmed:
            for event_type, confidence in confirmed.items():
                self._active.confidence_by_type[event_type] = max(
                    confidence,
                    self._active.confidence_by_type.get(event_type, 0.0),
                )
            self._active.last_seen_monotonic = observed_at
        elif self._active is not None:
            emitted.extend(self._close_if_inactive(observed_at))

        if self._active is None and confirmed:
            emitted.append(
                self._start(confirmed, occurred_at, observed_at, session_id)
            )

        if self._active is not None and confirmed:
            emitted.extend(self._split_if_due())
        return emitted

    def _clear_history(self) -> None:
        for values in self._history.values():
            values.clear()

    def _confirmed_candidates(
        self, candidates: Mapping[str, float]
    ) -> Dict[str, float]:
        for event_type in self._allowed:
            raw_confidence = candidates.get(event_type)
            confidence: Optional[float] = None
            if raw_confidence is not None:
                numeric = float(raw_confidence)
                if math.isfinite(numeric) and 0.0 <= numeric <= 1.0:
                    confidence = numeric
            self._history[event_type].append(confidence)

        confirmed: Dict[str, float] = {}
        for event_type, values in self._history.items():
            present = [value for value in values if value is not None]
            if len(present) >= self._required:
                confirmed[event_type] = max(present)
        return confirmed

    def _start(
        self,
        confirmed: Mapping[str, float],
        occurred_at: float,
        observed_at: float,
        session_id: str,
    ) -> EventClipBoundary:
        primary_type = self._primary_type(confirmed)
        last_notification = self._last_notification_monotonic.get(primary_type)
        notification_eligible = (
            last_notification is None
            or observed_at - last_notification >= self._notification_cooldown
        )
        if notification_eligible:
            self._last_notification_monotonic[primary_type] = observed_at
        event_group_id = self._group_id_factory(max(0, int(occurred_at * 1000)))
        self._active = _ActiveSegment(
            event_group_id=event_group_id,
            segment_index=0,
            detected_at_wall=occurred_at,
            segment_start_wall=occurred_at - self._pre_roll,
            segment_start_monotonic=observed_at - self._pre_roll,
            last_seen_monotonic=observed_at,
            confidence_by_type=dict(confirmed),
            session_ids={session_id},
            notification_eligible=notification_eligible,
        )
        return self._boundary("started")

    def _close_if_inactive(self, observed_at: float) -> List[EventClipBoundary]:
        assert self._active is not None
        end_monotonic = self._active.last_seen_monotonic + self._merge_gap
        if observed_at < end_monotonic:
            return []
        ended = self._boundary("ended", end_monotonic=end_monotonic)
        self._active = None
        self._clear_history()
        return [ended]

    def _split_if_due(self) -> List[EventClipBoundary]:
        assert self._active is not None
        segment_end = self._active.segment_start_monotonic + self._max_segment
        if self._active.last_seen_monotonic < segment_end:
            return []
        ended = self._boundary("ended", end_monotonic=segment_end)
        previous = self._active
        next_start_wall = previous.segment_start_wall + self._max_segment
        self._active = _ActiveSegment(
            event_group_id=previous.event_group_id,
            segment_index=previous.segment_index + 1,
            detected_at_wall=next_start_wall,
            segment_start_wall=next_start_wall,
            segment_start_monotonic=segment_end,
            last_seen_monotonic=previous.last_seen_monotonic,
            confidence_by_type=dict(previous.confidence_by_type),
            session_ids=set(previous.session_ids),
            notification_eligible=False,
            clock_stepped=previous.clock_stepped,
        )
        return [ended, self._boundary("started")]

    @staticmethod
    def _primary_type(confidence_by_type: Mapping[str, float]) -> str:
        return sorted(
            confidence_by_type,
            key=lambda value: (-_TYPE_PRIORITY.get(value, 0), value),
        )[0]

    def _boundary(
        self,
        phase: str,
        *,
        end_monotonic: Optional[float] = None,
    ) -> EventClipBoundary:
        assert self._active is not None
        labels = tuple(
            sorted(
                self._active.confidence_by_type,
                key=lambda value: (-_TYPE_PRIORITY.get(value, 0), value),
            )
        )
        primary_type = labels[0]
        duration_ms: Optional[int] = None
        end_at: Optional[float] = None
        if end_monotonic is not None:
            duration = max(
                0.0, end_monotonic - self._active.segment_start_monotonic
            )
            duration_ms = round(duration * 1000)
            end_at = self._active.segment_start_wall + duration
        raw_key = (
            f"{self._device_id}:{self._active.event_group_id}:"
            f"{self._active.segment_index}:{phase}"
        )
        return EventClipBoundary(
            phase=phase,
            event_group_id=self._active.event_group_id,
            segment_index=self._active.segment_index,
            primary_type=primary_type,
            labels=labels,
            confidence=self._active.confidence_by_type[primary_type],
            detected_at=self._active.detected_at_wall,
            start_at=self._active.segment_start_wall,
            end_at=end_at,
            monotonic_duration_ms=duration_ms,
            boot_id=self._boot_id,
            session_ids=tuple(sorted(self._active.session_ids)),
            clock_stepped_during_event=self._active.clock_stepped,
            notification_eligible=self._active.notification_eligible,
            idempotency_key=hashlib.sha256(raw_key.encode("utf-8")).hexdigest(),
        )
