"""Bounded, camera-confirmed positioning using the existing person follower."""

import math


class ConversationApproach:
    """Stop after one approach; never chase, search, or use bearing-only depth."""

    def __init__(self, started, desired_distance):
        self.started = started
        self.desired_distance = desired_distance
        self.last_seen = None
        self.target = None
        self.robot = None
        self.track = None
        self.aligned_since = None

    def expired(self, now):
        if now - self.started >= 20:
            return 'conversation positioning timed out'
        if self.last_seen is None:
            if now - self.started >= 3:
                return 'no metric person observation'
        elif now - self.last_seen > .6:
            return 'person observation lost'
        return None

    def observe(self, now, target, robot, track, *, metric, age=0.):
        """Accept only the same stationary, nearby person in map coordinates."""
        reason = self.expired(now)
        if reason:
            return reason
        values = (target.x, target.y, robot.x, robot.y)
        if (not metric or not all(math.isfinite(v) for v in values) or not track
                or not math.isfinite(age) or not 0 <= age <= .6):
            return 'person distance or track is uncertain'
        if self.track is not None and track != self.track:
            return 'person track changed'
        if self.target is not None and math.hypot(
                target.x - self.target.x, target.y - self.target.y) > .35:
            return 'person moved away from the conversation spot'
        if self.robot is not None and math.hypot(
                robot.x - self.robot.x, robot.y - self.robot.y) > 2:
            return 'conversation positioning travel limit'
        if self.target is None:
            if math.hypot(target.x - robot.x, target.y - robot.y) > 3:
                return 'person is outside conversation approach range'
            self.target, self.robot, self.track = target, robot, track
        self.last_seen = now - age
        return None

    def reached(self, now, distance, angle):
        """Require a stable distance and heading, then stop owned Nav2 motion."""
        good = (self.last_seen is not None and self.expired(now) is None
                and math.isfinite(distance) and math.isfinite(angle)
                and abs(distance - self.desired_distance) <= .2 and abs(angle) <= .1)
        if not good:
            self.aligned_since = None
            return False
        if self.aligned_since is None:
            self.aligned_since = now
        return now - self.aligned_since >= .5
