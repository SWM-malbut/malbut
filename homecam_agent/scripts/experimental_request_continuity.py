"""Offline duplicate-call probe, not identity recognition or a production scheduler.

Keep original Pose IDs/evidence intact. Only a repeated found_down request with
continuous matching head observations and a repeatedly restored body can attach
to an earlier request. New descent, missing/ambiguous evidence or recovery wins.
"""
import copy
from dataclasses import dataclass
import math

from experimental_request_dedup import RequestDedupConfig, _geometry


@dataclass(frozen=True)
class ContinuityConfig:
    max_age_sec: float = 5.0
    max_frame_gap_sec: float = .30
    max_body_gap_sec: float = .50
    min_body_samples: int = 3
    min_body_span_sec: float = .40
    min_box_iou: float = .80
    max_head_distance_eyes: float = .75
    max_joint_distance_body: float = .08

    def __post_init__(self):
        for value in vars(self).values():
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError('invalid continuity limit')
        if (type(self.min_body_samples) is not int or self.min_body_samples < 3
                or self.min_box_iou > 1 or self.max_age_sec > 5):
            raise ValueError('unsafe continuity limit')


HEAD = ('nose', 'left_eye', 'right_eye')
TORSO = ('left_shoulder', 'right_shoulder', 'left_hip', 'right_hip')


def head_matches(a, b, config):
    if any(p not in a or p not in b for p in HEAD):
        return False
    scale = math.dist(a['left_eye'], a['right_eye'])
    return scale >= 3 and all(math.dist(a[n], b[n]) <=
                              scale*config.max_head_distance_eyes for n in HEAD)


def overlap(a, b):
    area_a, area_b = (a[2]-a[0])*(a[3]-a[1]), (b[2]-b[0])*(b[3]-b[1])
    inter = max(0, min(a[2], b[2])-max(a[0], b[0])) * max(
        0, min(a[3], b[3])-max(a[1], b[1]))
    return inter/(area_a+area_b-inter)


def body_matches(reference, current, observation, config):
    a, pa = reference
    b, pb = current
    if not observation['features'].get('horizontal') or overlap(a, b) < config.min_box_iou:
        return False
    if not head_matches(pa, pb, config) or any(n not in pa or n not in pb for n in TORSO):
        return False
    scale = math.hypot(a[2]-a[0], a[3]-a[1])
    return all(math.dist(pa[n], pb[n]) <= scale*config.max_joint_distance_body for n in TORSO)


def noncontradictory_head_fragment(reference, fragment, config):
    """A weak nested fragment cannot prove continuity; it may only avoid a false conflict.

    The caller must independently observe a complete strong matching head this frame.
    """
    box, points = reference
    small, partial = fragment
    available = [n for n in HEAD if n in partial]
    if not available or any(n not in points for n in HEAD):
        return False
    area = (small[2]-small[0])*(small[3]-small[1])
    whole = (box[2]-box[0])*(box[3]-box[1])
    intersection = max(0, min(box[2], small[2])-max(box[0], small[0])) * max(
        0, min(box[3], small[3])-max(box[1], small[1]))
    scale = math.dist(points['left_eye'], points['right_eye'])
    center = ((small[0]+small[2])/2, (small[1]+small[3])/2)
    return (area/whole <= .35 and intersection/area >= .9 and scale >= 3
            and math.dist(center, points['nose']) <= 2*scale
            and all(math.dist(points[n], partial[n]) <=
                    scale*config.max_head_distance_eyes for n in available))


class ContinuityProbe:
    """A causal, clip-scoped proposal for *new requests* only.

    Not an alert suppressor: cannot consume VLM/voice responses or infer normality.
    Existing verification updates remain unchanged outside this proposal ledger.
    """
    def __init__(self, config=None):
        self.config = config or ContinuityConfig()
        self.geometry_config = RequestDedupConfig()
        self.roots = {}
        self.last = None
        self.size = None
        self.row = None
        self.geometry = {}

    def observe(self, row, size):
        time, frame = row['timestamp_s'], row['frame_index']
        if (not math.isfinite(time) or type(frame) is not int or frame < 0
                or len(size) != 2 or any(type(v) is not int or v <= 0 for v in size)):
            raise ValueError('invalid frame')
        clock_break = self.last and (
            time <= self.last[0] or frame <= self.last[1]
            or time-self.last[0] > self.config.max_frame_gap_sec+1e-9)
        reset = (clock_break or self.size is not None and size != self.size
                 or row['fall_analysis']['status'] != 'ok')
        if reset:
            self.roots.clear()
        self.last, self.size, self.row = (time, frame), size, row
        observations = row['observations']
        ids = [o['track_id'] for o in observations if o['track_id'] is not None]
        if len(ids) != len(set(ids)):
            raise ValueError('duplicate observation assignment')
        valid = [(o, _geometry(o, size, self.geometry_config)) for o in observations]
        valid = [(o, g) for o, g in valid if g is not None]
        self.geometry = {o['track_id']: (o, g) for o, g in valid if o['track_id'] is not None}
        # Contradictory or multiple tracking assignments must never strengthen a match.
        states = {d['targetTrackId']: d['trackingState'] for d in row['fall_analysis']['tracks']}
        for rid, root in list(self.roots.items()):
            if time-root['time'] > self.config.max_age_sec:
                del self.roots[rid]
                continue
            box, points = root['geometry']
            matches = [(o, g) for o, g in valid if head_matches(points, g[1], self.config)]
            # A visible head is required at EVERY sampled frame; absence is not continuity.
            if not matches or any(states.get(o['track_id']) == 'ambiguous' for o, _ in matches):
                del self.roots[rid]
                continue
            conflict = False
            for o, g in valid:
                if any(o is m for m, _ in matches):
                    continue
                if (overlap(box, g[0]) > .1
                        and not noncontradictory_head_fragment(root['geometry'], g, self.config)):
                    conflict = True
            # A matched body leaving the old lying configuration is new state, not a duplicate.
            for o, g in matches:
                if (o['features'].get('usable') and overlap(box, g[0]) > .5
                        and not body_matches(root['geometry'], g, o, self.config)):
                    conflict = True
            if conflict:
                del self.roots[rid]
                continue
            root['head_frames'].append(frame)
            for o, g in matches:
                tid = o['track_id']
                if tid is None or not body_matches(root['geometry'], g, o, self.config):
                    continue
                samples = root['body_frames'].setdefault(tid, [])
                if samples and time-samples[-1][1] > self.config.max_body_gap_sec+1e-9:
                    samples.clear()
                samples.append((frame, time))

    def route(self, request):
        row = self.row
        candidate = request['origin']['candidate']
        tid = candidate['targetTrackId']
        rid = request['request_id']
        default = dict(action='request', parent_request_id=rid, original=copy.deepcopy(request))
        # Postponed or historical requests are outside this strictly current-evidence probe.
        if (row['fall_analysis']['status'] != 'ok' or request['dispatch_kind'] != 'request'
                or request['decision_frame_index'] != row['frame_index']
                or abs(request['dispatch_time_s']-row['timestamp_s']) > 1e-9
                or candidate.get('observationAvailability') == 'missing'):
            return default
        if candidate['candidateKind'] != 'found_down':
            self.roots.clear()  # any new descent must bypass duplicate suppression
            return default
        if candidate.get('evidence', {}).get('robotMotion') not in (None, 'unknown', 'stationary'):
            self.roots.clear()
            return default
        current = self.geometry.get(tid)
        if current is None:
            return default
        obs, geometry = current
        options = []
        for root_id, root in self.roots.items():
            samples = root['body_frames'].get(tid, [])
            if (tid != root['track_id'] and len(samples) >= self.config.min_body_samples
                    and samples[-1][0] == row['frame_index']
                    and samples[-1][1]-samples[0][1]+1e-9 >= self.config.min_body_span_sec
                    and body_matches(root['geometry'], geometry, obs, self.config)):
                options.append((root_id, root, samples))
        if len(options) == 1:
            root_id, root, samples = options[0]
            return dict(action='attach_evidence', parent_request_id=root_id,
                        original=copy.deepcopy(request), proof=dict(
                            basis='continuous_head_and_repeated_same_body_configuration',
                            original_track_id=root['track_id'], new_track_id=tid,
                            original_frame=root['frame'],
                            head_observed_frames=root['head_frames'][:],
                            restored_body_frames=[f for f, _ in samples],
                            identity_verified=False, normality_inferred=False,
                            limitation='2D development probe; no appearance/re-ID '
                                       'or camera compensation'))
        if (obs['features'].get('horizontal') and all(n in geometry[1] for n in (*HEAD, *TORSO))):
            self.roots[rid] = dict(track_id=tid, time=row['timestamp_s'], frame=row['frame_index'],
                                   geometry=copy.deepcopy(geometry),
                                   head_frames=[row['frame_index']], body_frames={})
        return default
