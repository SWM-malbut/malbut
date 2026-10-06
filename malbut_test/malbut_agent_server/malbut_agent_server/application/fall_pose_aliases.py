"""Narrow repair of tracker ID churn during continuously measured low posture.

Not re-identification: no gap/occlusion, recovery, weak or competing observations
may establish an alias. Bounds are experimental, not measured identity accuracy.
"""

from collections import deque
from dataclasses import replace

from malbut_agent_server.application.fall_cloud_association import box_iou
from malbut_agent_server.domain.fall_monitoring import SubjectCheckState, SubjectFrame


class LowPoseAliases:
    max_gap_s = .25
    min_iou = .80
    competitor_iou = .20
    minimum_prior_samples = 3

    def __init__(self):
        self.evidence = deque(maxlen=256)
        self.clear()

    def clear(self):
        self.previous = None
        self.names = {}
        self.runs = {}
        self.evidence.clear()

    @staticmethod
    def low(pose):
        return (pose.association_usable and pose.box is not None
                and pose.state is SubjectCheckState.SUSPECTED)

    def observe(self, frame, *, stationary, motion_keys=()):
        if not isinstance(frame, SubjectFrame):
            raise ValueError('invalid subject frame')
        previous = self.previous
        if previous is not None and frame.observed_at <= previous.observed_at:
            raise ValueError('non-increasing subject frame')
        old = {p.subject_key: p for p in previous.subjects} if previous else {}
        new = {p.subject_key: p for p in frame.subjects}
        names = {key: self.names.get(key, key) for key in new}
        repairs, evidence = {}, []
        continuous = (previous is not None and stationary
                      and 0 < frame.observed_at - previous.observed_at
                      <= min(self.max_gap_s, frame.max_gap_s, previous.max_gap_s) + 1e-9)
        if continuous:
            for key, pose in new.items():
                if key in old or key in motion_keys or not self.low(pose):
                    continue
                # All boxes compete, including weak/unknown or still-present IDs.
                prior = [k for k, p in old.items() if p.box is not None
                         and box_iou(p.box, pose.box) >= self.competitor_iou]
                if len(prior) != 1:
                    continue
                source = prior[0]
                before = old[source]
                if (source in new or not self.low(before)
                        or self.runs.get(source, 0) < self.minimum_prior_samples
                        or box_iou(before.box, pose.box) < self.min_iou):
                    continue
                current = [k for k, p in new.items() if p.box is not None
                           and box_iou(p.box, before.box) >= self.competitor_iou]
                if current != [key]:
                    continue
                canonical = self.names[source]
                # A retired raw ID returning beside its current alias is not
                # permission to give two people one canonical ID.
                if any(other != key and value == canonical for other, value in names.items()):
                    continue
                names[key] = canonical
                repairs[key] = source
                evidence.append(dict(observed_at=frame.observed_at,
                    previous_at=previous.observed_at, previous_key=source,
                    observed_key=key, canonical_key=canonical,
                    iou=box_iou(before.box, pose.box), reason='continuous_low_pose_id_change'))
        # Never invent duplicate identities when a previously aliased raw ID
        # reappears. The adapter invalidates evidence on rejection.
        if len(set(names.values())) != len(names):
            raise ValueError('ambiguous canonical pose identity')
        runs = {}
        for key, pose in new.items():
            prior_key = repairs.get(key, key)
            runs[key] = ((self.runs.get(prior_key, 0) if continuous
                          and prior_key in old and self.low(old[prior_key]) else 0) + 1
                         if self.low(pose) else 0)
        self.previous, self.names, self.runs = frame, names, runs
        self.evidence.extend(evidence)
        return replace(frame, subjects=tuple(replace(p, subject_key=names[p.subject_key])
                                             for p in frame.subjects))

    def key(self, raw):
        return self.names.get(raw, raw)
