"""Offline request routing with bounded uncertainty; never merges Pose IDs.

Input candidate evidence and its source frame stay immutable. Dispatch time is
separate. The caller must tick poll() even if frames stop, and flush() on exit.
"""
from collections import deque
import copy
from dataclasses import dataclass, replace
from itertools import combinations
import math

from experimental_request_dedup import (
    RequestDedupConfig, RequestDedupExperiment, _geometry, pair_evidence,
)


@dataclass(frozen=True)
class CoalescingConfig(RequestDedupConfig):
    box_margin_fraction: float = .05
    minimum_raw_containment: float = .85
    evidence_window_sec: float = 2.0
    pending_enabled: bool = True
    pending_minimum_samples: int = 2
    pending_minimum_span_sec: float = .2
    maximum_pending_sec: float = .5
    maximum_pending_items: int = 32

    def __post_init__(self):
        # The legacy config validates its own numeric fields only.
        RequestDedupConfig(**{k: getattr(self, k)
                              for k in RequestDedupConfig.__dataclass_fields__})
        for name in ('box_margin_fraction', 'minimum_raw_containment', 'evidence_window_sec',
                     'pending_minimum_span_sec', 'maximum_pending_sec'):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError('invalid '+name)
        if (self.box_margin_fraction > .1
                or not .8 <= self.minimum_raw_containment <= self.minimum_containment
                or self.evidence_window_sec < self.minimum_span_sec
                or self.pending_minimum_span_sec > self.minimum_span_sec
                or self.maximum_pending_sec > .5
                or type(self.pending_enabled) is not bool
                or type(self.pending_minimum_samples) is not int
                or not 2 <= self.pending_minimum_samples <= self.minimum_samples
                or type(self.maximum_pending_items) is not int
                or not 1 <= self.maximum_pending_items <= 32):
            raise ValueError('invalid coalescing limits')


def tolerant_pair(a, b, size, config):
    """Keep original joints/boxes; padding is only a bounded comparison margin."""
    proof = pair_evidence(a, b, size, config)
    if proof is not None:
        return dict(proof, raw_containment=proof['containment'],
                    padded_containment=proof['containment'], margin_px=[0.0, 0.0])
    proof = pair_evidence(a, b, size, replace(
        config, minimum_containment=config.minimum_raw_containment))
    if proof is None:
        return None
    ba, _ = _geometry(a, size, config)
    bb, _ = _geometry(b, size, config)
    small, large = sorted((ba, bb), key=lambda v: (v[2]-v[0])*(v[3]-v[1]))
    sw, sh = small[2]-small[0], small[3]-small[1]
    mx, my = sw*config.box_margin_fraction, sh*config.box_margin_fraction
    padded = max(0, min(small[2], large[2]+mx)-max(small[0], large[0]-mx)) * max(
        0, min(small[3], large[3]+my)-max(small[1], large[1]-my)) / (sw*sh)
    if padded < config.minimum_containment:
        return None
    return dict(proof, raw_containment=proof['containment'], padded_containment=padded,
                margin_px=[mx, my])


class CorrespondenceTracker(RequestDedupExperiment):
    """Legacy routing constraints with actual-sample retention across short gaps."""
    def __init__(self, config):
        super().__init__(config)
        self.confirmed_routes = {}

    def _relation(self, candidate, request, row):
        tid = candidate['targetTrackId']
        route = self.confirmed_routes.get(tid)
        continuing = route is not None and self.requests.get(route) is request
        # Keep the age limit for new associations, not for a continuously proven
        # route. Original request/evidence timestamps themselves never change.
        checked = dict(request, timestamp_s=row['timestamp_s']) if continuing else request
        proof = super()._relation(candidate, checked, row)
        if proof is not None:
            proof['requestAgePolicy'] = ('continued_fresh_direct_correspondence' if continuing
                                         else 'new_relation_within_request_age_limit')
            proof['originalRequestTimestampSec'] = request['timestamp_s']
        return proof

    def _observe(self, row, image_size):
        stamp, frame = row['timestamp_s'], row['frame_index']
        if (not math.isfinite(stamp) or type(frame) is not int or frame < 0
                or len(image_size) != 2 or any(type(v) is not int or v <= 0 for v in image_size)):
            raise ValueError('invalid frame metadata')
        previous_frame, previous_time = self.last_frame, self.last_time
        continuous = previous_time is None or stamp > previous_time and frame > previous_frame
        if (not continuous or self.image_size is not None and self.image_size != image_size
                or previous_time is not None
                and stamp-previous_time > self.config.maximum_sample_gap_sec+1e-9):
            self.pairs.clear()
            self.latest.clear()
            self.confirmed_routes.clear()
            self.epoch += 1
        self.last_time, self.last_frame, self.image_size = stamp, frame, image_size
        analysis = row['fall_analysis']
        states = {d['targetTrackId']: d['trackingState'] for d in analysis['tracks']}
        valid = {'tracked', 'tentative'}
        if (not continuous or analysis['status'] != 'ok' or analysis['unassignedCount']
                or any(o['track_id'] is None for o in row['observations'])):
            self.pairs.clear()
            self.latest.clear()
            self.confirmed_routes.clear()
            return []
        observations = {}
        for o in row['observations']:
            tid = o['track_id']
            if tid in observations:
                raise ValueError('duplicate observation assignment')
            if states.get(tid) in valid:
                observations[tid] = o
                self.latest[tid] = dict(frameIndex=frame, timestampSec=stamp,
                                        observationIndex=o['observation_index'])
        active = {tid for tid, state in states.items() if state in valid | {'missing'}}
        self.latest = {t: r for t, r in self.latest.items() if t in active
                       and stamp-r['timestampSec'] <= self.config.maximum_missing_sec+1e-9}
        matches, degree = {}, {}
        for a, b in combinations(sorted(observations), 2):
            proof = tolerant_pair(observations[a], observations[b], image_size, self.config)
            if proof:
                matches[a, b] = proof
                degree[a], degree[b] = degree.get(a, 0)+1, degree.get(b, 0)+1
        ambiguous = {t for t, n in degree.items() if n > 1}
        for pair in list(self.pairs):
            a, b = pair
            samples = self.pairs[pair]
            if (a not in self.latest or b not in self.latest or ambiguous.intersection(pair)
                    or a in observations and b in observations and pair not in matches
                    or stamp-samples[-1]['timestampSec'] > self.config.maximum_missing_sec+1e-9):
                del self.pairs[pair]
        for pair, proof in matches.items():
            if ambiguous.intersection(pair):
                continue
            samples = self.pairs.setdefault(pair, deque(maxlen=32))
            while samples and stamp-samples[0]['timestampSec'] > self.config.evidence_window_sec:
                samples.popleft()
            adjacent = (samples and samples[-1]['frameIndex'] == previous_frame
                        and stamp-samples[-1]['timestampSec']
                        <= self.config.maximum_sample_gap_sec+1e-9)
            samples.append(dict(frameIndex=frame, timestampSec=stamp,
                                observedAdjacentSec=(stamp-samples[-1]['timestampSec']
                                                     if adjacent else 0.0),
                                observations={t: dict(self.latest[t]) for t in pair}, **proof))
        for tid, rid in list(self.confirmed_routes.items()):
            root = self.requests[rid]
            pair = tuple(sorted((tid, root['track_id'])))
            samples = self.pairs.get(pair)
            if (self.routes.get(tid) != rid or root['epoch'] != self.epoch
                    or not samples or not self._confirmed(samples)):
                del self.confirmed_routes[tid]
        return [dict(trackIds=list(pair), confirmed=self._confirmed(samples),
                     sampleFrames=[s['frameIndex'] for s in samples],
                     evidenceWindowSec=samples[-1]['timestampSec']-samples[0]['timestampSec'],
                     observedAdjacentSec=sum(s['observedAdjacentSec'] for s in list(samples)[1:]))
                for pair, samples in sorted(self.pairs.items())]

    def possible_root(self, candidate, row):
        """Unconfirmed but unique current correspondence; not permission to merge."""
        tid, cfg = candidate['targetTrackId'], self.config
        if (candidate['candidateKind'] != 'found_down' or tid in self.routes
                or candidate.get('observationAvailability') == 'missing'):
            return None
        options = []
        for rid, root in self.requests.items():
            other = root['track_id']
            samples = self.pairs.get(tuple(sorted((tid, other))))
            if (other == tid or root['kind'] != 'found_down' or root['epoch'] != self.epoch
                    or not 0 <= row['timestamp_s']-root['timestamp_s']
                    <= cfg.maximum_request_age_sec or not samples):
                continue
            if (len(samples) < cfg.pending_minimum_samples
                    or samples[-1]['timestampSec']-samples[0]['timestampSec']+1e-9
                    < cfg.pending_minimum_span_sec
                    or any(self.latest.get(t, {}).get('frameIndex') != row['frame_index']
                           for t in (tid, other))):
                continue
            options.append(rid)
        return options[0] if len(options) == 1 else None

    def route(self, candidate, original_update, row, *, separate=False, origin_stamp=None):
        """Same direct-routing contract as legacy, without observing a frame twice."""
        tid = candidate['targetTrackId']
        route, root, proof = self.routes.get(tid), None, None
        if not separate:
            root = self.requests.get(route)
            if root and root['track_id'] != tid:
                proof = self._relation(candidate, root, row)
                if proof is None:
                    root = None
            if root is None:
                options = [(rid, req, self._relation(candidate, req, row))
                           for rid, req in self.requests.items()]
                options = [(rid, req, p) for rid, req, p in options if p is not None]
                if len(options) == 1:
                    route, root, proof = options[0]
        if root is None:
            route = candidate['candidateId']
            if route in self.requests:
                raise ValueError('candidate ID reused')
            stamp = row['timestamp_s'] if origin_stamp is None else origin_stamp
            self.requests[route] = dict(track_id=tid, kind=candidate['candidateKind'],
                                        timestamp_s=stamp, epoch=self.epoch)
            self.routes[tid] = route
            self.confirmed_routes.pop(tid, None)
            return 'request', route, None
        if root['track_id'] == tid and candidate['candidateKind'] == 'fall_suspected':
            root['kind'] = 'fall_suspected'
        self.routes[tid] = route
        update = copy.deepcopy(original_update) if original_update else dict(evidence=candidate)
        update['requestCandidateId'] = route
        if proof is not None:
            proof['basis'] = 'bounded_margin_repeated_head_and_arm_correspondence'
            update['deduplication'] = proof
            self.confirmed_routes[tid] = route
        return 'update', route, update


class RequestCoalescer:
    """Explicit dispatch envelopes prevent delayed evidence from being backdated."""
    def __init__(self, config=None):
        self.config = config or CoalescingConfig()
        self.tracker = CorrespondenceTracker(self.config)
        self.pending = []
        self.last_now = None
        self.last_row = None
        self.details = []

    def _clock(self, now_s):
        if (not math.isfinite(now_s) or self.last_now is not None and now_s < self.last_now):
            raise ValueError('dispatch clock must be monotonic')
        self.last_now = now_s

    def _dispatch(self, item, row, now_s, reason, separate=False):
        origin = item['origin']
        kind, route, update = self.tracker.route(
            origin['candidate'], origin['original_update'], row, separate=separate,
            origin_stamp=origin['timestamp_s'])
        return dict(origin=copy.deepcopy(origin), dispatch_kind=kind, request_id=route,
                    update=update, dispatch_time_s=now_s,
                    decision_frame_index=(None if reason in {'deadline', 'end_of_stream'}
                                          else row['frame_index']),
                    last_observation_frame_index=row['frame_index'],
                    delay_sec=now_s-item['enqueued_at'], reason=reason)

    def poll(self, now_s):
        self._clock(now_s)
        output = []
        for item in list(self.pending):
            if now_s+1e-9 >= item['deadline']:
                output.append(self._dispatch(item, self.last_row, now_s, 'deadline', True))
                self.pending.remove(item)
        return output

    def flush(self, now_s):
        output = self.poll(now_s)
        for item in self.pending:
            output.append(self._dispatch(item, self.last_row, now_s, 'end_of_stream', True))
        self.pending.clear()
        return output

    def update(self, row, *, image_size, now_s):
        output = self.poll(now_s)
        self.last_row = copy.deepcopy(row)
        self.details = self.tracker._observe(row, image_size)
        for item in list(self.pending):
            candidate = item['origin']['candidate']
            root = self.tracker.requests[item['root']]
            pair = tuple(sorted((candidate['targetTrackId'], root['track_id'])))
            if self.tracker._relation(candidate, root, row) is not None:
                output.append(self._dispatch(item, row, now_s, 'confirmed_after_wait'))
            elif (pair not in self.tracker.pairs or root['epoch'] != self.tracker.epoch
                  or row['timestamp_s']-root['timestamp_s'] > self.config.maximum_request_age_sec):
                output.append(self._dispatch(item, row, now_s, 'correspondence_lost', True))
            else:
                continue
            self.pending.remove(item)
        items = [(c, None) for c in row['fall_analysis']['candidates']]
        items.extend((u['evidence'], u) for u in row.get('verification_updates', []))
        for index, (candidate, original_update) in enumerate(items):
            tid = candidate['targetTrackId']
            # New evidence from the same track cannot disappear behind its queue entry.
            for waiting in list(self.pending):
                if waiting['origin']['candidate']['targetTrackId'] == tid:
                    output.append(self._dispatch(waiting, row, now_s, 'new_evidence', True))
                    self.pending.remove(waiting)
            origin = dict(case_id=row.get('case_id'), frame_index=row['frame_index'],
                          timestamp_s=row['timestamp_s'], item_index=index,
                          candidate=copy.deepcopy(candidate),
                          original_update=copy.deepcopy(original_update))
            item = dict(origin=origin, enqueued_at=now_s)
            root = self.tracker.possible_root(candidate, row)
            confirmed = root is not None and self.tracker._relation(
                candidate, self.tracker.requests[root], row) is not None
            if (self.config.pending_enabled and root is not None and not confirmed
                    and len(self.pending) < self.config.maximum_pending_items):
                item.update(root=root, deadline=now_s+self.config.maximum_pending_sec)
                self.pending.append(item)
            else:
                output.append(self._dispatch(item, row, now_s, 'immediate'))
        return output
