"""Offline request coalescing, NOT a person identity or non-fall classifier.

Track IDs and original evidence remain intact. Matching is deliberately limited
to repeatedly co-observed head + arm in nested full/partial Pose boxes. No image
labels, motion prerequisite, transitive aliases, VLM calls or ROS dependencies.
"""
from collections import deque
import copy
from dataclasses import asdict, dataclass
from itertools import combinations
import math


@dataclass(frozen=True)
class RequestDedupConfig:
    minimum_containment: float = .9
    maximum_area_ratio: float = .5
    minimum_keypoint_confidence: float = .65
    maximum_head_distance: float = .08
    maximum_arm_distance: float = .1
    minimum_samples: int = 3
    minimum_span_sec: float = .3
    maximum_sample_gap_sec: float = .3
    maximum_missing_sec: float = .5
    maximum_request_age_sec: float = 2.0

    def __post_init__(self):
        for name, value in asdict(self).items():
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'invalid {name}')
            if ('containment' in name or 'ratio' in name or 'confidence' in name
                    or 'distance' in name) and value > 1:
                raise ValueError(f'invalid {name}')
        if type(self.minimum_samples) is not int or not 3 <= self.minimum_samples <= 32:
            raise ValueError('invalid minimum_samples')
        if not self.maximum_sample_gap_sec <= self.maximum_missing_sec:
            raise ValueError('sample gap exceeds missing allowance')


def _geometry(observation, size, config):
    pose = observation['pose']
    b = [pose['box'][k] for k in ('left', 'top', 'right', 'bottom')]
    if (not all(math.isfinite(v) and 0 <= v <= 1 for v in b)
            or b[0] >= b[2] or b[1] >= b[3]):
        return None
    w, h = size
    box = b[0]*w, b[1]*h, b[2]*w, b[3]*h
    points = {}
    names = set()
    for p in pose['keypoints']:
        if p['name'] in names:
            return None
        names.add(p['name'])
        if (all(math.isfinite(p[k]) and 0 <= p[k] <= 1 for k in ('x', 'y', 'confidence'))
                and p['confidence'] >= config.minimum_keypoint_confidence
                and b[0] <= p['x'] <= b[2] and b[1] <= p['y'] <= b[3]):
            points[p['name']] = p['x']*w, p['y']*h
    return box, points


def pair_evidence(a, b, image_size, config):
    """Actual same-frame correspondence. Confidence alone is never sufficient."""
    ga, gb = _geometry(a, image_size, config), _geometry(b, image_size, config)
    if ga is None or gb is None:
        return None
    ba, pa = ga
    bb, pb = gb
    area_a, area_b = (ba[2]-ba[0])*(ba[3]-ba[1]), (bb[2]-bb[0])*(bb[3]-bb[1])
    small = ba if area_a <= area_b else bb
    ratio = min(area_a, area_b) / max(area_a, area_b)
    intersection = max(0, min(ba[2], bb[2])-max(ba[0], bb[0])) * max(
        0, min(ba[3], bb[3])-max(ba[1], bb[1]))
    containment = intersection / min(area_a, area_b)
    if ratio > config.maximum_area_ratio or containment < config.minimum_containment:
        return None
    diagonal = math.hypot(small[2]-small[0], small[3]-small[1])

    def distances(names):
        if any(name not in pa or name not in pb for name in names):
            return None
        return {name: math.dist(pa[name], pb[name])/diagonal for name in names}

    head = distances(('nose', 'left_eye', 'right_eye'))
    if head is None or max(head.values()) > config.maximum_head_distance:
        return None
    arms = {}
    for side in ('left', 'right'):
        d = distances(tuple(f'{side}_{part}' for part in ('shoulder', 'elbow', 'wrist')))
        if d is not None and max(d.values()) <= config.maximum_arm_distance:
            arms[side] = d
    if not arms:
        return None
    return dict(containment=containment, area_ratio=ratio,
                normalized_head_distances=head, normalized_arm_distances=arms)


class RequestDedupExperiment:
    """Clip-scoped first requests, with short-lived DIRECT cross-track evidence.

    A merge creates an evidence update, not an erased candidate. If that track
    later emits evidence without a valid relation, give it its own request.
    Product acknowledgement, escalation and incident closure remain out of scope.
    """
    def __init__(self, config=None):
        self.config = config or RequestDedupConfig()
        self.pairs = {}
        self.latest = {}
        self.routes = {}
        self.requests = {}
        self.last_time = None
        self.last_frame = None
        self.image_size = None
        self.epoch = 0

    def _observe(self, row, image_size):
        stamp, frame = row['timestamp_s'], row['frame_index']
        if (not math.isfinite(stamp) or type(frame) is not int or frame < 0
                or len(image_size) != 2 or any(type(v) is not int or v <= 0 for v in image_size)):
            raise ValueError('invalid frame metadata')
        continuous = (self.last_time is None or
                      stamp > self.last_time and frame > self.last_frame)
        previous_frame = self.last_frame
        if not continuous or self.image_size is not None and self.image_size != image_size:
            self.pairs.clear()
            self.latest.clear()
            self.epoch += 1
        self.last_time, self.last_frame, self.image_size = stamp, frame, image_size
        analysis = row['fall_analysis']
        states = {d['targetTrackId']: d['trackingState'] for d in analysis['tracks']}
        valid = {'tracked', 'tentative'}
        if (not continuous or analysis['status'] != 'ok' or analysis['unassignedCount']
                or any(o['track_id'] is None for o in row['observations'])):
            self.pairs.clear()
            self.latest.clear()
            return []
        observations = {}
        for o in row['observations']:
            tid = o['track_id']
            if tid in observations:
                raise ValueError('multiple observations assigned to one track')
            if states.get(tid) in valid:
                observations[tid] = o
                self.latest[tid] = dict(frameIndex=frame, timestampSec=stamp,
                                        observationIndex=o['observation_index'])
        active = {tid for tid, state in states.items() if state in valid | {'missing'}}
        self.latest = {t: r for t, r in self.latest.items() if t in active
                       and stamp-r['timestampSec'] <= self.config.maximum_missing_sec + 1e-9}
        matches = {}
        for a, b in combinations(sorted(observations), 2):
            proof = pair_evidence(observations[a], observations[b], image_size, self.config)
            if proof:
                matches[a, b] = proof
        # Reject even one-frame third-person ambiguity, rather than choosing rank 1.
        degree = {}
        for a, b in matches:
            degree[a], degree[b] = degree.get(a, 0)+1, degree.get(b, 0)+1
        ambiguous = {t for t, n in degree.items() if n > 1}
        for pair in list(self.pairs):
            a, b = pair
            if (a not in self.latest or b not in self.latest or ambiguous.intersection(pair)
                    or a in observations and b in observations and pair not in matches
                    or stamp-self.pairs[pair][-1]['timestampSec']
                    > self.config.maximum_missing_sec + 1e-9):
                del self.pairs[pair]
        for pair, proof in matches.items():
            if ambiguous.intersection(pair):
                continue
            samples = self.pairs.setdefault(pair, deque(maxlen=32))
            # Missing time is never counted as observed agreement.
            if samples and (samples[-1]['frameIndex'] != previous_frame or
                            stamp-samples[-1]['timestampSec'] >
                            self.config.maximum_sample_gap_sec + 1e-9):
                samples.clear()
            samples.append(dict(frameIndex=frame, timestampSec=stamp,
                                observations={t: dict(self.latest[t]) for t in pair}, **proof))
        return [dict(trackIds=list(pair), confirmed=self._confirmed(samples),
                     sampleFrames=[s['frameIndex'] for s in samples])
                for pair, samples in sorted(self.pairs.items())]

    def _confirmed(self, samples):
        return (len(samples) >= self.config.minimum_samples and
                samples[-1]['timestampSec']-samples[0]['timestampSec'] + 1e-9
                >= self.config.minimum_span_sec)

    def _relation(self, candidate, request, row):
        tid, other = candidate['targetTrackId'], request['track_id']
        stamp = row['timestamp_s']
        if (tid == other or candidate['candidateKind'] != 'found_down'
                or request['kind'] != 'found_down'
                or request['epoch'] != self.epoch
                or not 0 <= stamp-request['timestamp_s'] <= self.config.maximum_request_age_sec):
            return None
        pair = tuple(sorted((tid, other)))
        samples = self.pairs.get(pair)
        if not samples or not self._confirmed(samples):
            return None
        if any(t not in self.latest for t in pair):
            return None
        if candidate.get('observationAvailability') == 'missing':
            ref = candidate.get('evidenceReference')
            if ref != self.latest[tid] or ref != samples[-1]['observations'][tid]:
                return None
        elif self.latest[tid]['frameIndex'] != row['frame_index']:
            return None
        # A newly observed requester cannot borrow an old peer position.
        if self.latest[tid] != samples[-1]['observations'][tid]:
            return None
        return dict(basis='repeated_nested_head_and_arm_correspondence',
                    trackIds=list(pair), currentPoseAvailable={
                        t: self.latest[t]['frameIndex'] == row['frame_index'] for t in pair},
                    actualSamples=copy.deepcopy(list(samples)),
                    uncertainty='experimental_2d_correspondence_not_verified_identity')

    def update(self, row, *, image_size):
        details = self._observe(row, image_size)
        output, updates = [], []
        items = [(c, None) for c in row['fall_analysis']['candidates']]
        items.extend((u['evidence'], u) for u in row.get('verification_updates', []))
        for candidate, original_update in items:
            tid = candidate['targetTrackId']
            route = self.routes.get(tid)
            root = self.requests.get(route)
            proof = None
            if root and root['track_id'] != tid:
                proof = self._relation(candidate, root, row)
                if proof is None:
                    root = None
            if root is None:
                # Never merge transitively through a member: only the original target.
                options = [(rid, req, self._relation(candidate, req, row))
                           for rid, req in self.requests.items()]
                options = [(rid, req, p) for rid, req, p in options if p is not None]
                if len(options) == 1:
                    route, root, proof = options[0]
                else:
                    route = candidate['candidateId']
                    root = dict(track_id=tid, kind=candidate['candidateKind'],
                                timestamp_s=row['timestamp_s'], epoch=self.epoch)
                    if route in self.requests:
                        raise ValueError('candidate ID reused for another request')
                    self.requests[route] = root
                    self.routes[tid] = route
                    output.append(copy.deepcopy(candidate))
                    continue
            self.routes[tid] = route
            if root['track_id'] == tid and candidate['candidateKind'] == 'fall_suspected':
                root['kind'] = 'fall_suspected'
            update = copy.deepcopy(original_update) if original_update else dict(evidence=candidate)
            update['requestCandidateId'] = route
            if proof is not None:
                update['deduplication'] = proof
            updates.append(copy.deepcopy(update))
        return output, updates, details
