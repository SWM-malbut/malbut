"""Offline horizontal-descent evidence and bounded incident rechecks.

Not fall confirmation, identity recognition or a deployed notification worker.
Only observed keypoints enter the detector. GT, RGB predictions and model labels
are deliberately absent from its interface. A normal VLM answer cannot discard
evidence that arrived after its input snapshot.
"""
from collections import deque
import copy
from dataclasses import dataclass
import math
from statistics import median

from experimental_partial_pose import PartialConfig, geometry


HEAD = ('nose', 'left_eye', 'right_eye')
TORSO = ('left_shoulder', 'right_shoulder', 'left_hip', 'right_hip')
DROP = 'horizontal_head_body_descent_needs_review'
LOST = 'low_pose_then_unobserved'


@dataclass(frozen=True)
class DropConfig:
    window_s: float = 2.0
    maximum_gap_s: float = .5
    minimum_reference_span_s: float = .15
    minimum_drop_heights: float = .25
    minimum_relative_drop_heights: float = .15
    minimum_speed_heights_s: float = .20
    minimum_references: int = 2

    def __post_init__(self):
        for v in vars(self).values():
            if isinstance(v, bool) or not math.isfinite(v) or v <= 0:
                raise ValueError('invalid drop config')
        if (type(self.minimum_references) is not int or self.minimum_references < 2
                or self.maximum_gap_s > self.window_s
                or self.minimum_reference_span_s > self.window_s):
            raise ValueError('invalid drop windows')


def horizontal_descent(before, after, config):
    """Downward head movement relative to hips, not whole-image translation.

    This can indicate rolling/sliding from a lying position. It cannot determine
    whether the support is a bed or floor, nor distinguish every voluntary roll.
    """
    pa, pb = before['points'], after['points']
    if not before['horizontal'] or any(n not in pa or n not in pb for n in HEAD+TORSO):
        return None
    dt = after['time']-before['time']
    if not config.minimum_reference_span_s <= dt <= config.window_s:
        return None
    height = before['box'][3]-before['box'][1]
    a_head, b_head = median(pa[n][1] for n in HEAD), median(pb[n][1] for n in HEAD)
    a_hip = (pa['left_hip'][1]+pa['right_hip'][1])/2
    b_hip = (pb['left_hip'][1]+pb['right_hip'][1])/2
    drop = (b_head-a_head)/height
    relative = ((b_head-b_hip)-(a_head-a_hip))/height
    # Bound gross scale/identity jumps; do not infer missing joints.
    width_ratio = (after['box'][2]-after['box'][0])/(before['box'][2]-before['box'][0])
    if (not .5 <= width_ratio <= 2 or drop < config.minimum_drop_heights
            or relative < config.minimum_relative_drop_heights
            or drop/dt < config.minimum_speed_heights_s):
        return None
    return dict(referenceFrame=before['frame'], referenceTimeSec=before['time'],
                headDropBoxHeights=drop, relativeToHipsBoxHeights=relative,
                headDropSpeedBoxHeightsPerSec=drop/dt)


class HorizontalDrop:
    def __init__(self, config=None):
        self.config = config or DropConfig()
        # Same experimental 0.10 candidate floor; strong joints still required.
        self.geometry_config = PartialConfig(minimum_detection=.10)
        self.history, self.emitted = {}, {}
        self.last = self.size = self.case = None

    def observe(self, row, size):
        stamp, frame = row['timestamp_s'], row['frame_index']
        if (type(frame) is not int or frame < 0 or not math.isfinite(stamp)
                or len(size) != 2 or any(type(v) is not int or v <= 0 for v in size)):
            raise ValueError('invalid frame')
        base = row.get('baseline_analysis', row['fall_analysis'])
        discontinuous = self.last and (
            stamp <= self.last[0] or frame <= self.last[1]
            or stamp-self.last[0] > self.config.maximum_gap_s+1e-9)
        reset = (row['case_id'] != self.case or discontinuous
                 or self.size is not None and size != self.size or base['status'] != 'ok'
                 or base['robotMotion'] not in ('unknown', 'stationary'))
        if reset:
            self.history.clear()
            self.emitted.clear()
        self.case, self.last, self.size = row['case_id'], (stamp, frame), size
        if discontinuous or base['status'] != 'ok' or base['robotMotion'] not in (
                'unknown', 'stationary'):
            return []
        tracks = {d['targetTrackId']: d for d in base['tracks']}
        observed = [o['track_id'] for o in row['observations'] if o['track_id'] is not None]
        if len(observed) != len(set(observed)):
            raise ValueError('duplicate tracking assignment')
        for tid in list(self.history):
            if (stamp-self.history[tid][-1]['time'] > self.config.maximum_gap_s+1e-9
                    or tracks.get(tid, {}).get('trackingState') not in
                    ('tracked', 'tentative', 'missing')):
                del self.history[tid]
        candidates = []
        for obs in row['observations']:
            tid = obs['track_id']
            diag = tracks.get(tid)
            if tid is None or not diag or diag['trackingState'] not in ('tracked', 'tentative'):
                continue
            sample = geometry(obs, self.geometry_config)
            if sample is None or any(n not in sample['points'] for n in HEAD+TORSO):
                continue
            sample.update(time=stamp, frame=frame,
                          horizontal=bool((diag['features'] or {}).get('horizontal')))
            history = self.history.setdefault(tid, deque(maxlen=32))
            while history and stamp-history[0]['time'] > self.config.window_s:
                history.popleft()
            proofs = [p for old in history if old['time'] > self.emitted.get(tid, -1)
                      if (p := horizontal_descent(old, sample, self.config)) is not None]
            history.append(sample)
            if (len(proofs) < self.config.minimum_references
                    or max(p['referenceTimeSec'] for p in proofs)
                    - min(p['referenceTimeSec'] for p in proofs)+1e-9
                    < self.config.minimum_reference_span_s):
                continue
            self.emitted[tid] = stamp
            candidates.append(dict(
                candidateId=f'horizontal-drop-{tid}-{frame}', revision=1,
                candidateKind='fall_suspected', source='yolo_pose', targetTrackId=tid,
                observationAvailability='observed', observationId=base['observationId'],
                evidenceStartSec=min(p['referenceTimeSec'] for p in proofs),
                evidenceEndSec=stamp, emittedAtSec=stamp, requiresVerification=True,
                reasons=[DROP], uncertainties=[
                    'not_confirmed_fall', 'lying_surface_unknown', 'voluntary_roll_possible',
                    'weak_detection_permitted', 'camera_motion_uncompensated',
                    'track_id_is_not_verified_identity'],
                evidence=dict(pose=copy.deepcopy(diag['features']),
                              robotMotion=base['robotMotion'], temporal=dict(
                                  basis=DROP, referenceSamples=proofs,
                                  observedFrame=frame, currentPoseObserved=True))))
        return candidates


@dataclass(frozen=True)
class RecheckConfig:
    post_change_s: float = .8
    minimum_interval_s: float = 1.0
    maximum_rechecks: int = 2

    def __post_init__(self):
        if (type(self.maximum_rechecks) is not int or not 1 <= self.maximum_rechecks <= 4
                or any(isinstance(v, bool) or not math.isfinite(v) or v <= 0
                       for v in (self.post_change_s, self.minimum_interval_s))):
            raise ValueError('invalid recheck config')


class IncidentRechecks:
    """Explicit completion callbacks; a pending request is never overwritten.

    IDs remain clip-scoped incident keys, not human IDs. Time must be monotonic.
    A caller ticks even without Pose detections, retaining available RGB. Help
    requests/alerts and incident closure are outside this experimental component.
    """
    def __init__(self, config=None):
        self.config = config or RecheckConfig()
        self.incidents, self.ledger = {}, []
        self.last_time = None

    def _clock(self, now):
        if not math.isfinite(now) or self.last_time is not None and now < self.last_time:
            raise ValueError('nonmonotonic clock')
        self.last_time = now

    def ingest(self, event, *, stable_head=False):
        now, rid = event['dispatch_time_s'], event['request_id']
        self._clock(now)
        c = event['origin']['candidate']
        if c.get('emittedAtSec', c['evidenceEndSec']) > now+1e-9:
            raise ValueError('future evidence')
        if rid not in self.incidents:
            if event['dispatch_kind'] != 'request':
                raise ValueError('update without incident')
            self.incidents[rid] = dict(
                kind=c['candidateKind'], initial=copy.deepcopy(event), count=0,
                inflight=None, last_call=-math.inf, pending=[], consumed=set(),
                review_required=False)
            return
        s = self.incidents[rid]
        reasons = []
        if c['candidateKind'] == 'fall_suspected' and s['kind'] != 'fall_suspected':
            reasons.append('new_fall_motion')
            s['kind'] = 'fall_suspected'
        if DROP in c['reasons']:
            reasons.append(DROP)
        if LOST in c['reasons'] and not stable_head:
            reasons.append(LOST)
        for reason in reasons:
            # A repeated identical warning is not new evidence of another incident.
            identity = (reason, c['candidateId']) if reason == DROP else (reason,)
            if identity in s['consumed'] or any(p['identity'] == identity for p in s['pending']):
                continue
            s['pending'].append(dict(identity=identity, reason=reason,
                                     time=now, event=copy.deepcopy(event)))
            self.ledger.append(dict(incident_id=rid, action='important_change',
                                    reason=reason, time_s=now))
        if not reasons:
            self.ledger.append(dict(incident_id=rid, action='evidence_only', time_s=now,
                                    reason='stable_visible_head' if stable_head else
                                    'no_new_important_change'))

    def tick(self, now, frame, frame_time):
        self._clock(now)
        if type(frame) is not int or frame < 0 or not 0 <= frame_time <= now+1e-9:
            raise ValueError('future/invalid RGB frame')
        output = []
        for rid, s in self.incidents.items():
            if s['inflight'] is not None:
                continue
            first = s['count'] == 0
            if not first:
                if not s['pending']:
                    continue
                if s['count'] > self.config.maximum_rechecks:
                    if not s['review_required']:
                        self.ledger.append(dict(incident_id=rid, action='review_required',
                                                time_s=now, reason='recheck_limit'))
                    s['review_required'] = True
                    continue
                due = max(min(p['time'] for p in s['pending'])+self.config.post_change_s,
                          s['last_call']+self.config.minimum_interval_s)
                if now+1e-9 < due:
                    continue
                # No new RGB after the previous snapshot: leave it unresolved.
                if frame <= s['last_frame']:
                    continue
            changes = copy.deepcopy(s['pending'])
            s['consumed'].update(p['identity'] for p in changes)
            s['pending'].clear()
            s['count'] += 1
            call_id = f'{rid}:check-{s["count"]}'
            s.update(inflight=call_id, last_call=now, last_frame=frame)
            output.append(dict(
                call_id=call_id, incident_id=rid, sequence=s['count'],
                dispatch_time_s=now, available_through_frame=frame,
                frame_time_s=frame_time, changes=changes,
                initial=copy.deepcopy(s['initial']),
                mode='initial' if first else 'recheck'))
        return output

    def complete(self, call_id, now, outcome):
        self._clock(now)
        if outcome not in ('normal', 'needs_check', 'failed'):
            raise ValueError('invalid outcome')
        matches = [s for s in self.incidents.values() if s['inflight'] == call_id]
        if len(matches) != 1:
            raise ValueError('unknown/stale completion')
        s = matches[0]
        s['inflight'] = None
        # Normal applies only to that snapshot. Pending changes survive all outcomes.
        self.ledger.append(dict(call_id=call_id, action='completed', time_s=now,
                                outcome=outcome, pending_changes=len(s['pending'])))
