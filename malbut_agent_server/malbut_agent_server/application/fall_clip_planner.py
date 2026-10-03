"""Per-incident recording ranges for fall event clips; metadata only, no pixels.

Each observation is an evidence window on the monitor's monotonic clock. Its
clip is the window widened by ``pre_s`` before and ``post_s`` after. Ranges of
one incident extend while they touch and are split into segments of at most
``max_segment_s``. Times stay monotonic here; the monitor converts them to wall
time when it records them, because the recording archive is indexed by wall time.
"""

from dataclasses import dataclass, replace
import math
from typing import Dict, List, Tuple

from malbut_agent_server.domain.fall_monitoring import identifier, timestamp

ANCHOR_KINDS = ('pose_motion', 'pose_found_down', 'cloud_window')


@dataclass(frozen=True)
class ClipSegment:
    incident_id: str
    segment_index: int
    start: float
    end: float
    revision: int
    anchor_kinds: Tuple[str, ...]
    found_down: bool


@dataclass(frozen=True)
class RecordedClip:
    """A segment as recorded: wall-clock seconds for the recording archive."""
    incident_id: str
    boot_id: str
    segment_index: int
    revision: int
    start_at: float
    end_at: float
    anchor_kinds: Tuple[str, ...]
    found_down: bool
    clock_stepped: bool


class FallClipPlanner:
    def __init__(self, *, pre_s: float = 10.0, post_s: float = 20.0,
                 max_segment_s: float = 120.0, max_segments: int = 32,
                 min_segment_s: float = 1.0) -> None:
        for value in (pre_s, post_s):
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError('clip margins must be finite and non-negative')
        if (type(max_segment_s) not in (int, float) or not math.isfinite(max_segment_s)
                or max_segment_s <= pre_s + post_s):
            raise ValueError('max_segment_s must exceed the clip margins')
        if type(max_segments) is not int or max_segments < 1:
            raise ValueError('max_segments must be a positive integer')
        self.pre_s, self.post_s = float(pre_s), float(post_s)
        self.max_segment_s, self.max_segments = float(max_segment_s), max_segments
        # A split remainder shorter than this is dropped, never sent as an empty range.
        self.min_segment_s = float(min_segment_s)
        self.dropped_s = 0.0
        self._segments: Dict[str, List[ClipSegment]] = {}

    def segments(self, incident_id: str) -> Tuple[ClipSegment, ...]:
        return tuple(self._segments.get(incident_id, ()))

    def forget(self, incident_id: str) -> None:
        self._segments.pop(incident_id, None)

    def observe(self, incident_id: str, evidence_start: float, evidence_end: float, *,
                anchor_kind: str, found_down: bool = False) -> Tuple[ClipSegment, ...]:
        """Return the segments created or changed by this evidence window."""
        identifier(incident_id)
        timestamp(evidence_start)
        timestamp(evidence_end)
        if evidence_start > evidence_end:
            raise ValueError('evidence window is reversed')
        if anchor_kind not in ANCHOR_KINDS:
            raise ValueError('unsupported clip anchor')
        start = max(0.0, evidence_start - self.pre_s)
        end = evidence_end + self.post_s
        found_down = bool(found_down) or anchor_kind == 'pose_found_down'
        segments = self._segments.setdefault(incident_id, [])
        changed: List[ClipSegment] = []
        if len(segments) > 1 and end < segments[-1].start:
            # Earlier than the last segment: older segments already cover that
            # part of the incident; never attach it to an unrelated segment.
            return ()
        if segments and start <= segments[-1].end:
            last = segments[-1]
            # Earlier evidence may widen the first segment only; later segments
            # already belong to an older part of the same incident.
            new_start = min(last.start, start) if len(segments) == 1 else last.start
            new_end = max(last.end, end)
            kinds = last.anchor_kinds + (() if anchor_kind in last.anchor_kinds
                                         else (anchor_kind,))
            limit = new_start + self.max_segment_s
            updated = replace(last, start=new_start, end=min(new_end, limit),
                              anchor_kinds=kinds, found_down=last.found_down or found_down)
            if updated != last:
                segments[-1] = replace(updated, revision=last.revision + 1)
                changed.append(segments[-1])
            start = limit
            end = new_end
            if start >= end:
                return tuple(changed)
        while start < end and len(segments) < self.max_segments:
            if end - start < self.min_segment_s:
                break
            segment = ClipSegment(
                incident_id, len(segments), start, min(end, start + self.max_segment_s),
                1, (anchor_kind,), found_down)
            segments.append(segment)
            changed.append(segment)
            start = segment.end
        if start < end:
            self.dropped_s += end - start
        return tuple(changed)
