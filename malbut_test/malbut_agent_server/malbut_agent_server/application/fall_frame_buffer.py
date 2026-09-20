"""Bounded in-memory RGB history populated independently of YOLO results."""

from collections import deque
from threading import Lock

from malbut_agent_server.domain.fall_monitoring import (
    FrameWindow,
    RgbFrame,
    positive,
    timestamp,
)


class FallFrameBuffer:
    """Stores immutable JPEGs; no disk archive or cloud upload here.

    One instance belongs to one camera/device boot. Call clear on camera OFF
    or restart. Snapshot consumers must release their byte references too.
    """

    def __init__(self, *, retention_s: float, max_bytes: int,
                 max_frames: int) -> None:
        positive(retention_s, 'retention_s')
        for value in (max_bytes, max_frames):
            if type(value) is not int or value < 1:
                raise ValueError('invalid capacity')
        self.retention_s = retention_s
        self.max_bytes = max_bytes
        self.max_frames = max_frames
        self._frames = deque()
        self._size = 0
        self._last_stamp = -1.0
        self._lock = Lock()

    def append(self, frame: RgbFrame) -> None:
        if not isinstance(frame, RgbFrame):
            raise ValueError('invalid frame')
        if len(frame.jpeg) > self.max_bytes:
            raise ValueError('frame exceeds byte capacity')
        with self._lock:
            if frame.captured_at <= self._last_stamp:
                raise ValueError('non-increasing frame time')
            self._last_stamp = frame.captured_at
            self._frames.append(frame)
            self._size += len(frame.jpeg)
            self._evict(frame.captured_at)

    def _evict(self, now: float) -> None:
        while self._frames and (
            self._frames[0].captured_at < now - self.retention_s
            or self._size > self.max_bytes
            or len(self._frames) > self.max_frames
        ):
            self._size -= len(self._frames.popleft().jpeg)

    def window(self, *, end: float, duration_s: float,
               max_images: int, max_age_s: float) -> FrameWindow:
        timestamp(end)
        positive(duration_s, 'duration_s')
        positive(max_age_s, 'max_age_s')
        if type(max_images) is not int or max_images < 1:
            raise ValueError('invalid image count')
        start = max(0.0, end - duration_s)
        with self._lock:
            self._evict(end)
            values = tuple(f for f in self._frames
                           if start <= f.captured_at <= end)
        if not values or end - values[-1].captured_at > max_age_s:
            raise ValueError('fresh RGB unavailable')
        incomplete = values[0].captured_at > start
        if len(values) > max_images:
            if max_images == 1:
                values = values[-1:]
            else:
                values = tuple(values[round(i * (len(values) - 1)
                                            / (max_images - 1))]
                               for i in range(max_images))
        return FrameWindow(values, start, end, incomplete)

    def clear(self) -> None:
        with self._lock:
            self._frames.clear()
            self._size = 0
            self._last_stamp = -1.0

    @property
    def stored_bytes(self) -> int:
        with self._lock:
            return self._size
