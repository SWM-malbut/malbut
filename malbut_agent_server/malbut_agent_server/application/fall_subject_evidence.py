"""Bound measured target boxes to RGB timestamps without interpolating people.

Any missed/weak/ambiguous association starts a new continuity token. Tokens are
not person identity or human/fall evidence and are never sent to Cloud.
"""

from collections import OrderedDict
from uuid import uuid4

from malbut_agent_server.domain.fall_monitoring import (
    SubjectFrame, SubjectVideoTarget,
)


class FallSubjectEvidence:
    def __init__(self, *, retention_s, max_frames):
        self.retention_s, self.max_frames = retention_s, max_frames
        self.active = False
        self._frames = OrderedDict()
        self._tokens = {}
        self._gap = None
        self.generation = 0

    def clear(self):
        self.generation += 1
        self._frames.clear()
        self._tokens.clear()
        self._gap = None

    def append(self, frame: SubjectFrame):
        if not isinstance(frame, SubjectFrame):
            raise ValueError('invalid subject frame')
        self.active = True
        previous = next(reversed(self._frames), None)
        if previous is not None and frame.observed_at <= previous:
            raise ValueError('out-of-order subject frame')
        if (self._gap != frame.max_gap_s
                or (previous is not None and frame.observed_at - previous > frame.max_gap_s)):
            self._tokens.clear()
        self._gap = frame.max_gap_s
        tokens, entries = {}, {}
        for pose in frame.subjects:
            if pose.association_usable:
                tokens[pose.subject_key] = self._tokens.get(pose.subject_key) or uuid4().hex
            entries[pose.subject_key] = (tokens.get(pose.subject_key), pose)
        self._tokens = tokens
        self._frames[frame.observed_at] = entries
        while (len(self._frames) > self.max_frames
               or next(iter(self._frames)) < frame.observed_at - self.retention_s):
            self._frames.popitem(last=False)

    def target(self, subject_key, window):
        entries = [self._frames.get(f.captured_at, {}).get(subject_key) for f in window.frames]
        if (not entries or any(e is None or e[0] is None for e in entries)
                or len({e[0] for e in entries}) != 1):
            return None
        return SubjectVideoTarget(
            subject_key, entries[0][0], tuple(f.captured_at for f in window.frames),
            tuple(e[1].box for e in entries))

    def latest(self, subject_key):
        stamp = next(reversed(self._frames), None)
        entry = self._frames[stamp].get(subject_key) if stamp is not None else None
        return (stamp, *entry) if entry is not None else None

    def token_at(self, subject_key, observed_at):
        entry = self._frames.get(observed_at, {}).get(subject_key)
        return entry[0] if entry is not None else None

    def frames_since(self, observed_at):
        """Measured subjects in frames at or after this time, oldest first."""
        return tuple((stamp, tuple(pose for _, pose in entries.values()))
                     for stamp, entries in self._frames.items() if stamp >= observed_at)

    def at(self, observed_at):
        """Exact measured sample only; never backfill with a nearby Pose box."""
        return tuple((key, token, pose) for key, (token, pose) in
                     self._frames.get(observed_at, {}).items())

    def observed_through(self, observed_at):
        """Whether ordered Pose input has reached this RGB's timestamp."""
        latest = next(reversed(self._frames), None)
        return latest is not None and latest >= observed_at

    def snapshot(self, window):
        """Immutable measured boxes at dispatch; no interpolation or RGB copy."""
        return tuple(tuple((key, token, pose) for key, (token, pose) in
                           self._frames.get(frame.captured_at, {}).items())
                     for frame in window.frames)

    def association_samples(self, sample_times, *, tolerance_s=.1):
        """Exact observations, or BOTH neighbors within 100ms; no interpolation.

        Preserve explicit empty/weak observations. A missing timestamp is not
        permission to extrapolate from only one side or carry a box forward.
        The caller must verify the same unambiguous token in every sample.
        This tolerance is a development bound, not validated robot accuracy.
        Exact-only target selection/normal closure above are unchanged.
        """
        if not 0 <= tolerance_s <= .1:
            raise ValueError('association tolerance must be within 100ms')
        stamps = tuple(self._frames)
        result = []
        for stamp in sample_times:
            if stamp in self._frames:
                result.append(((stamp, self.at(stamp)),))
                continue
            before = next((t for t in reversed(stamps) if t < stamp), None)
            after = next((t for t in stamps if t > stamp), None)
            if (before is None or after is None or tolerance_s == 0
                    or stamp - before > tolerance_s + 1e-6
                    or after - stamp > tolerance_s + 1e-6):
                result.append(())
            else:
                result.append(((before, self.at(before)), (after, self.at(after))))
        return tuple(result)
