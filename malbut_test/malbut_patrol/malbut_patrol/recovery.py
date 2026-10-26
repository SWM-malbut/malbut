"""Bounded recovery toward recently traversed poses, using Nav2 for motion."""

from collections import deque
import math


def angle_difference(first, second):
    """Return the shortest signed heading change."""
    return math.atan2(math.sin(first-second), math.cos(first-second))


class BreadcrumbTrail:
    """Discard history after a localization jump, long gap, or new patrol."""

    def __init__(self):
        self.samples = deque(maxlen=256)
        self.anchor = None

    def clear(self):
        self.samples.clear()
        self.anchor = None

    def consistent(self, anchor):
        return (self.anchor is not None
                and math.dist(anchor[:2], self.anchor[:2]) <= .12
                and abs(angle_difference(anchor[2], self.anchor[2])) <= .15)

    def record(self, pose, anchor, now):
        if (not self.consistent(anchor)
                or (self.samples and now-self.samples[-1][0] > 1.0)):
            self.clear()
        if self.anchor is None:
            self.anchor = anchor
        while self.samples and now-self.samples[0][0] > 60.:
            self.samples.popleft()
        # Retain stationary freshness without allowing idle samples to evict
        # the travelled path. Turns retain their original body orientation.
        if (self.samples and math.dist(pose[:2], self.samples[-1][1][:2]) < .03
                and abs(angle_difference(pose[2], self.samples[-1][1][2])) < .08):
            self.samples[-1] = now, self.samples[-1][1]
        else:
            self.samples.append((now, pose))

    def retreat(self, current, now, maximum_distance=1.5):
        """Return recent poses in reverse travel order, within a local budget."""
        if not self.samples or now-self.samples[-1][0] > 1.0:
            return []
        if math.dist(current[:2], self.samples[-1][1][:2]) > .15:
            return []
        poses, distance = [current], 0.
        for stamp, pose in reversed(self.samples):
            if now-stamp > 60.:
                break
            step = math.dist(pose[:2], poses[-1][:2])
            if distance+step > maximum_distance:
                break
            if step > .005:
                poses.append(pose)
                distance += step
        return poses if distance >= .35 else []


def path_segments(poses, trail, maximum_length=3., maximum_switches=4):
    """
    Validate a local SE2 plan and split it at forward/reverse cusps.

    Nav2's footprint-aware planner and live controller own collision checks.
    This envelope prevents an escape plan taking an unrelated detour or an
    unbounded sequence of shunts. Replanning never edits the obstacle map.
    """
    if len(poses) < 2 or not trail:
        raise ValueError('No recent recovery path')
    if not all(math.isfinite(v) for pose in poses for v in pose):
        raise ValueError('Nonfinite recovery path')
    if math.dist(poses[0][:2], trail[0][:2]) > .15:
        raise ValueError('Recovery path starts away from the robot')
    if math.dist(poses[-1][:2], trail[-1][:2]) > .10:
        raise ValueError('Recovery path does not reach its breadcrumb')
    segments, segment, direction, length = [], [0], None, 0.
    for i in range(1, len(poses)):
        before, after = poses[i-1], poses[i]
        dx, dy = after[0]-before[0], after[1]-before[1]
        step = math.hypot(dx, dy)
        if step > .20:
            raise ValueError('Recovery path has a discontinuity')
        length += step
        if min(math.dist(after[:2], point[:2]) for point in trail) > .35:
            raise ValueError('Recovery leaves the recently traversed area')
        if step < .001:
            continue
        projection = dx*math.cos(before[2]) + dy*math.sin(before[2])
        if abs(projection) < .5*step:
            raise ValueError('Recovery path requires lateral motion')
        next_direction = 1 if projection >= 0 else -1
        if direction is not None and next_direction != direction:
            segments.append((direction, segment))
            segment = [i-1]
        direction = next_direction
        segment.append(i)
    if direction is not None:
        segments.append((direction, segment))
    if length > maximum_length or len(segments) > maximum_switches+1 or not segments:
        raise ValueError('Recovery manoeuvre budget exceeded')
    if not any(direction < 0 for direction, _ in segments):
        raise ValueError('Recovery plan does not backtrack')
    return segments
