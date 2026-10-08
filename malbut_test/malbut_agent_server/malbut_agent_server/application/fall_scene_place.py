"""Whether a Cloud scene finding continues an open case, NOT person identity.

A case is one continuing situation: the same place in the home, without a long
silence. Places compare as map points (AMCL pose + depth) when both sides have
them. Otherwise image positions are used, which are comparable only while the
camera stayed still; only movement the detector reported, or a reset of its
input, counts as movement. Development bounds, not measured accuracy.
"""

from collections import deque
import math

from malbut_agent_server.application.fall_cloud_association import box_iou

# 2026-10-07 user decisions: 1 m on the map, 10 minutes since the last finding.
SAME_PLACE_M = 1.0
CASE_GAP_S = 600.0
SAME_PLACE_IOU = 0.3
# Cloud may place one mistaken spot on neighbouring objects (2026-10-07: a gap,
# a bag and a beige object, centres 6-8% of the frame apart, IoU under 0.15).
SAME_PLACE_CENTRE = 0.15
# Merge movement marks closer than this into one interval.
MOVE_JOIN_S = 1.0


def _centre(box):
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def _near(a, b):
    (ax, ay), (bx, by) = _centre(a), _centre(b)
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** .5 <= SAME_PLACE_CENTRE


def near_on_map(previous, current):
    return any(math.dist(a, b) <= SAME_PLACE_M for a in previous for b in current)


def same_place(previous, current):
    """Unknown locations on either side cannot tell places apart in one view."""
    if not previous or not current:
        return True
    return any(box_iou(a, b) >= SAME_PLACE_IOU or _near(a, b)
               for a in previous for b in current)


class CameraMotionLog:
    """Monotonic intervals in which the camera moved or its motion is unknown."""

    def __init__(self, *, max_intervals=256):
        self._moves = deque()
        self._max = max_intervals
        self._last = None
        # Set while Pose input is reset; the span until the next frame is unknown.
        self._reset_from = None
        # Dropped history counts as movement up to this time.
        self._unknown_until = None

    def observe(self, observed_at, *, moving):
        if self._reset_from is not None:
            self._mark(self._reset_from, observed_at)
            self._reset_from = None
        if moving:
            self._mark(observed_at, observed_at)
        self._last = observed_at if self._last is None else max(self._last, observed_at)

    def unknown(self, at):
        """Pose input reset or camera off: anything may happen until the next frame."""
        if self._reset_from is None:
            self._reset_from = self._last if self._last is not None else at

    def moved_between(self, start, end):
        low, high = min(start, end), max(start, end)
        if self._reset_from is not None and high >= self._reset_from:
            return True
        if self._unknown_until is not None and low <= self._unknown_until:
            return True
        return any(s <= high and e >= low for s, e in self._moves)

    def _mark(self, start, end):
        if self._moves and start - self._moves[-1][1] <= MOVE_JOIN_S:
            previous = self._moves.pop()
            start, end = min(previous[0], start), max(previous[1], end)
        elif len(self._moves) >= self._max:
            dropped = self._moves.popleft()
            self._unknown_until = max(self._unknown_until or dropped[1], dropped[1])
        self._moves.append((start, end))
