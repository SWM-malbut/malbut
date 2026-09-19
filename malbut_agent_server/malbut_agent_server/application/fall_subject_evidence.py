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

    def clear(self):
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
