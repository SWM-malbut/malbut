"""Per-incident person boxes for the fall clip overlay; positions only, no pixels.

Pose boxes from every subject frame (up to 5/s, not thinned) and Cloud AI
locations are collected for each recorded clip segment on the monitor's
monotonic clock. A segment is finalized shortly after it ends and again when it
changes. Times are milliseconds from the segment start, so a wall clock step
never moves a box inside its clip. Track keys are one-way hashes: they only let
the web match the same person across linked incidents of one scene.
Weak detector boxes (clothes, bedding) are left out unless they are the
incident's own person.
"""

from collections import OrderedDict, deque
from dataclasses import dataclass
import hashlib
import math
from typing import Dict, Iterable, Optional, Tuple

from malbut_agent_server.domain.fall_monitoring import identifier, timestamp

Box = Tuple[float, float, float, float]
# (ms from the segment start, left, top, right, bottom in 1/1000 of the frame)
Sample = Tuple[int, int, int, int, int]


@dataclass(frozen=True)
class PeopleTrack:
    key: str
    target: bool
    samples: Tuple[Sample, ...]


@dataclass(frozen=True)
class RecordedPeople:
    incident_id: str
    boot_id: str
    segment_index: int
    revision: int
    tracks: Tuple[PeopleTrack, ...]
    cloud: Tuple[Sample, ...]
    truncated: bool


class _Segment:
    def __init__(self, incident_id: str, index: int, target: Optional[str],
                 start: float, end: float) -> None:
        self.incident_id, self.index, self.target = incident_id, index, target
        self.start, self.end = start, end
        # (subject key, box, strong): strong is None when the detector did not say.
        self.frames: Dict[float, Tuple[Tuple[str, Box, Optional[bool]], ...]] = {}
        self.cloud: Dict[float, Box] = {}
        self.dirty, self.truncated = True, False


def _mille(box: Box) -> Optional[Tuple[int, int, int, int]]:
    left, top, right, bottom = (round(v * 1000) for v in box)
    return (left, top, right, bottom) if left < right and top < bottom else None


class FallPeopleRecorder:
    def __init__(self, *, boot_id: str, history_s: float = 30.0, settle_s: float = 2.0,
                 keep_s: float = 600.0, max_tracks: int = 12, max_frames: int = 650,
                 max_cloud: int = 32, max_segments: int = 256) -> None:
        identifier(boot_id)
        for value in (history_s, settle_s, keep_s):
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError('people recorder times must be finite and non-negative')
        for value in (max_tracks, max_frames, max_cloud, max_segments):
            if type(value) is not int or value < 1:
                raise ValueError('people recorder limits must be positive integers')
        self.boot_id = boot_id
        self.history_s, self.settle_s = float(history_s), float(settle_s)
        self.keep_s = float(keep_s)
        self.max_tracks, self.max_frames = max_tracks, max_frames
        self.max_cloud, self.max_segments = max_cloud, max_segments
        # Pre-roll: a clip starts 10 s before its evidence, which arrives later.
        self._history = deque()
        self._segments: Dict[Tuple[str, int], _Segment] = {}
        # Last sent revision per segment, kept after the samples are released so
        # a late change never re-sends a partial segment under an old revision.
        self._revisions = OrderedDict()

    def observe(self, observed_at: float, people: Iterable[tuple]) -> None:
        """people: (subject key, box) or (subject key, box, strong)."""
        timestamp(observed_at)
        if self._history and observed_at <= self._history[-1][0]:
            return
        frame = tuple((p[0], p[1], p[2] if len(p) > 2 else None) for p in people)
        self._history.append((observed_at, frame))
        while observed_at - self._history[0][0] > self.history_s:
            self._history.popleft()
        for segment in self._segments.values():
            if segment.start <= observed_at <= segment.end:
                self._add(segment, observed_at, frame)

    def segment(self, incident_id: str, subject_key: Optional[str], segment_index: int,
                start: float, end: float) -> None:
        identifier(incident_id)
        timestamp(start)
        timestamp(end)
        key = (incident_id, segment_index)
        segment = self._segments.get(key)
        if segment is None:
            if key in self._revisions:
                return  # Released after it was sent; history alone would be partial.
            if len(self._segments) >= self.max_segments and not self._release_one():
                return
            segment = self._segments[key] = _Segment(incident_id, segment_index,
                                                     subject_key, start, end)
        else:
            segment.start, segment.end, segment.dirty = start, end, True
            segment.frames = {t: f for t, f in segment.frames.items() if start <= t <= end}
            segment.cloud = {t: b for t, b in segment.cloud.items() if start <= t <= end}
        for observed_at, frame in self._history:
            if start <= observed_at <= end and observed_at not in segment.frames:
                self._add(segment, observed_at, frame)

    def cloud(self, incident_id: str, observed_at: float, box: Box) -> None:
        timestamp(observed_at)
        for segment in self._segments.values():
            if (segment.incident_id == incident_id
                    and segment.start <= observed_at <= segment.end
                    and segment.cloud.get(observed_at) != box):
                if len(segment.cloud) >= self.max_cloud:
                    segment.truncated = True
                    continue
                segment.cloud[observed_at] = box
                segment.dirty = True

    def clear_history(self) -> None:
        """Camera or detection off: no pre-roll survives; open segments still finish."""
        self._history.clear()

    def due(self, now: float) -> Tuple[RecordedPeople, ...]:
        timestamp(now)
        ready = []
        for key, segment in list(self._segments.items()):
            if now < segment.end + self.settle_s:
                continue
            if segment.dirty:
                segment.dirty = False
                people = self._build(segment, self._revisions.get(key, 0) + 1)
                if people is not None:
                    self._revisions[key] = people.revision
                    self._revisions.move_to_end(key)
                    while len(self._revisions) > 4096:
                        self._revisions.popitem(last=False)
                    ready.append(people)
            elif now >= segment.end + self.keep_s:
                del self._segments[key]
        return tuple(ready)

    def _add(self, segment: _Segment, observed_at: float, frame) -> None:
        if len(segment.frames) >= self.max_frames:
            segment.truncated = True
            return
        segment.frames[observed_at] = frame
        segment.dirty = True

    def _release_one(self) -> bool:
        for key, segment in self._segments.items():
            if not segment.dirty and key in self._revisions:
                del self._segments[key]
                return True
        return False

    def _key(self, subject_key: str) -> str:
        return hashlib.sha256(f'{self.boot_id}\0{subject_key}'.encode()).hexdigest()[:12]

    def _build(self, segment: _Segment, revision: int) -> Optional[RecordedPeople]:
        span = round((segment.end - segment.start) * 1000)

        def ms(t):
            return min(span, max(0, round((t - segment.start) * 1000)))
        tracks: Dict[str, list] = {}
        for observed_at in sorted(segment.frames):
            for subject, box, strong in segment.frames[observed_at]:
                if strong is False and subject != segment.target:
                    continue
                mille = _mille(box)
                if mille is not None:
                    tracks.setdefault(subject, []).append((ms(observed_at),) + mille)
        # Over the limit: keep the incident's own person, then the longest tracks.
        kept = sorted(tracks, key=lambda s: (s != segment.target, -len(tracks[s])))
        kept = sorted(kept[:self.max_tracks], key=lambda s: tracks[s][0][0])
        cloud = tuple(sorted((ms(t),) + m for t, m in (
            (t, _mille(box)) for t, box in segment.cloud.items()) if m is not None))
        if not kept and not cloud:
            return None
        return RecordedPeople(
            segment.incident_id, self.boot_id, segment.index, revision,
            tuple(PeopleTrack(self._key(s), s == segment.target, tuple(tracks[s])) for s in kept),
            cloud, segment.truncated or len(tracks) > self.max_tracks)
