"""Translate existing homecam JSON into fall-core inputs on its owning loop.

No ROS imports, image inference, inferred identities, or notification decisions.
ROS capture timestamps are converted using the receiving node's ROS clock, not
by treating Unix/simulation seconds as local monotonic time.
"""

import json
import math
from collections import OrderedDict

from malbut_agent_server.domain.fall_monitoring import (
    CandidateKind, FallCandidate, PersonObservation, PersonVisibility,
    RgbFrame, SensorSummary, identifier, positive, timestamp,
    SubjectFrame, SubjectPose, SubjectCheckState,
)


def bounded_object(payload):
    if not isinstance(payload, str) or len(payload.encode()) > 131072:
        raise ValueError('detector message exceeds limit')

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate field')
            result[key] = value
        return result

    def invalid(_):
        raise ValueError('nonfinite input')
    result = json.loads(payload, object_pairs_hook=pairs, parse_constant=invalid)
    if not isinstance(result, dict):
        raise ValueError('expected object')
    return result


def ros_stamp(value):
    if not isinstance(value, dict) or set(value) != {'sec', 'nanosec'}:
        raise ValueError('invalid capture stamp')
    sec, nano = value['sec'], value['nanosec']
    if type(sec) is not int or sec < 0 or type(nano) is not int or not 0 <= nano < 10**9:
        raise ValueError('invalid capture stamp')
    return sec + nano / 1e9


class FallDetectorInput:
    def __init__(self, monitor, *, max_source_age_s, max_candidates=64):
        positive(max_source_age_s, 'max_source_age_s')
        if type(max_candidates) is not int or not 1 <= max_candidates <= 256:
            raise ValueError('invalid candidate limit')
        self.monitor = monitor
        self.max_source_age_s = max_source_age_s
        self.max_candidates = max_candidates
        self._last = {}
        self._revisions = OrderedDict()
        self._capture_times = OrderedDict()
        self._frame_id = None
        self._last_source_now = None
        self._generation = 0
        self._settings = dict(enabled=False, camera_enabled=False,
                              cloud_consent=False, connected=False)

    def configure(self, **settings):
        self.monitor.configure(**settings)
        if not settings['enabled'] or not settings['camera_enabled']:
            self._last.clear()
            self._revisions.clear()
            self._capture_times.clear()
            if self._settings['enabled'] and self._settings['camera_enabled']:
                self._generation += 1
        self._settings = dict(settings)

    def _time(self, capture, *, source_now, now, channel, frame_id):
        timestamp(source_now)
        timestamp(now)
        timestamp(capture)
        identifier(frame_id)
        if source_now < capture or source_now - capture > self.max_source_age_s:
            raise ValueError('future or stale capture')
        changed = ((self._frame_id is not None and self._frame_id != frame_id)
                   or (self._last_source_now is not None and source_now < self._last_source_now))
        if changed:
            # Cancel in-flight evidence and discard old pixels on a ROS clock
            # reset/camera switch, without pretending old incidents recovered.
            self.monitor.configure(enabled=False, camera_enabled=False,
                                   cloud_consent=False, connected=False)
            self.monitor.configure(**self._settings)
            self._last.clear()
            self._revisions.clear()
            self._capture_times.clear()
            self._generation += 1
        self._frame_id, self._last_source_now = frame_id, source_now
        if channel in self._last and capture <= self._last[channel]:
            raise ValueError('duplicate or out-of-order capture')
        # RGB and pose callbacks arrive separately. The same original image
        # stamp must bind to exactly the same local timestamp, not callback jitter.
        key = (frame_id, capture)
        converted = self._capture_times.get(key, now - (source_now - capture))
        timestamp(converted)
        self._capture_times[key] = converted
        while len(self._capture_times) > 2048:
            self._capture_times.popitem(last=False)
        self._last[channel] = capture
        return converted

    def rgb(self, jpeg, *, capture, frame_id, source_now, now):
        observed = self._time(capture, source_now=source_now, now=now,
                              channel='rgb', frame_id=frame_id)
        return self.monitor.ingest_rgb(RgbFrame(observed, jpeg))

    def poses(self, payload, *, source_now, now):
        data = bounded_object(payload)
        if data.get('schemaVersion') != 1:
            raise ValueError('unsupported detector schema')
        if data.get('status') != 'ok':
            return self.monitor.observe_person(PersonObservation(now, PersonVisibility.UNKNOWN))
        persons, unassigned = data.get('persons'), data.get('unassigned')
        if (not isinstance(persons, list) or len(persons) > self.max_candidates
                or not isinstance(unassigned, list) or len(unassigned) > self.max_candidates):
            raise ValueError('invalid person observations')
        seen = bool(unassigned)
        for person in persons:
            if (not isinstance(person, dict) or type(person.get('observed')) is not bool
                    or (person['observed'] and not isinstance(person.get('pose'), dict))):
                raise ValueError('invalid person observation')
            # A weak pose is enough to keep fast checks; it is NOT proof of a
            # human or of a fall. Lost tracks alone do not count as current sight.
            seen |= person['observed']
        observed = self._time(ros_stamp(data.get('captureStamp')), source_now=source_now,
                              now=now, channel='poses', frame_id=data.get('frameId'))
        return self.monitor.observe_person(PersonObservation(
            observed, PersonVisibility.SEEN if seen else PersonVisibility.NOT_SEEN))

    def candidates(self, payload, *, source_now, now):
        try:
            return self._candidates(payload, source_now=source_now, now=now)
        except (ValueError, TypeError, KeyError):
            self.monitor.invalidate_subject_input()
            raise

    def _candidates(self, payload, *, source_now, now):
        timestamp(source_now)
        timestamp(now)
        data = bounded_object(payload)
        if (data.get('schemaVersion') != 1 or data.get('timeBase') != 'ros_image_stamp'
                or not isinstance(data.get('candidates'), list)
                or len(data['candidates']) > self.max_candidates):
            raise ValueError('invalid candidate message')
        if data.get('status') != 'ok':
            self.monitor.invalidate_subject_input()
            return ()
        identifier(data.get('frameId'))
        subjects = None
        if 'subjectCheckVersion' in data:
            if (data['subjectCheckVersion'] != 1 or not isinstance(data.get('tracks'), list)
                    or len(data['tracks']) > self.max_candidates
                    or type(data.get('unassignedCount')) is not int or data['unassignedCount'] < 0):
                raise ValueError('invalid subject checks')
            capture = data.get('captureTimeSec')
            timestamp(capture)
            positive(data.get('subjectCheckMaxGapSec'), 'subject check gap')
            if capture > source_now or source_now - capture > self.max_source_age_s:
                raise ValueError('stale subject checks')
            subjects = []
            for track in data['tracks']:
                if not isinstance(track, dict) or not isinstance(track.get('subjectCheck'), dict):
                    raise ValueError('invalid subject check')
                identifier(track.get('targetTrackId'))
                usable = track.get('associationUsable')
                if type(usable) is not bool:
                    raise ValueError('invalid subject association')
                check = SubjectCheckState(track['subjectCheck'].get('state'))
                if usable and (track.get('trackingState') != 'tracked'
                               or not isinstance(track.get('features'), dict)
                               or track['features'].get('usable') is not True
                               or data['unassignedCount'] != 0):
                    raise ValueError('unsupported subject association')
                if check is SubjectCheckState.CLEAR and (
                        not usable or track['subjectCheck'].get('reason') != 'stable_upright'
                        or data.get('robotMotion') != 'stationary'):
                    raise ValueError('unsupported subject clearance')
                box = track.get('box')
                subjects.append(SubjectPose(
                    track['targetTrackId'], tuple(box) if isinstance(box, list) else box,
                    check, usable))
            # Validate duplicate identities and full payload before side effects.
            SubjectFrame(capture, tuple(subjects), data['subjectCheckMaxGapSec'])
        prepared = []
        # Validate the whole message before any event or question can be emitted.
        for item in data['candidates']:
            if (not isinstance(item, dict) or item.get('source') != 'yolo_pose'
                    or item.get('requiresVerification') is not True
                    or type(item.get('revision')) is not int or item['revision'] < 1):
                raise ValueError('invalid candidate')
            for field in ('targetTrackId', 'candidateId'):
                identifier(item.get(field))
                if len(item[field]) > 160:
                    raise ValueError('candidate ID too long')
            previous = self._revisions.get(item['candidateId'])
            if previous is not None and previous[1] != item['targetTrackId']:
                raise ValueError('candidate identity changed')
            kind = {'fall_suspected': CandidateKind.MOTION_SEEN,
                    'found_down': CandidateKind.ALREADY_DOWN}.get(item.get('candidateKind'))
            if kind is None:
                raise ValueError('unknown candidate kind')
            start, end = item.get('evidenceStartSec'), item.get('evidenceEndSec')
            timestamp(start)
            timestamp(end)
            if start > end or end > source_now or source_now - end > self.max_source_age_s:
                raise ValueError('stale candidate evidence')
            evidence = item.get('evidence')
            if not isinstance(evidence, dict) or not isinstance(evidence.get('pose'), dict):
                raise ValueError('invalid candidate evidence')
            floor = evidence['pose'].get('floor_height_m')
            if floor is not None and (
                    type(floor) not in (float, int) or not math.isfinite(floor) or floor < 0):
                raise ValueError('invalid floor distance')
            prepared.append((item, kind, end, floor))
        observed_frame = None
        if subjects is not None:
            observed_frame = self._time(
                capture, source_now=source_now, now=now, channel='subject_frame',
                frame_id=data['frameId'])
        result = []
        for item, kind, end, floor in prepared:
            key = item['candidateId']
            previous = self._revisions.get(key)
            if previous is not None:
                revision, track = previous
                if track != item['targetTrackId']:
                    raise ValueError('candidate identity changed')
                if item['revision'] <= revision:
                    continue
            observed = self._time(end, source_now=source_now, now=now,
                                  channel='candidate:' + item['targetTrackId'],
                                  frame_id=data.get('frameId'))
            subject = f'pose:{self._generation}:{item["targetTrackId"]}'
            sensor = SensorSummary(observed, floor_distance_m=floor) if floor is not None else None
            iid = self.monitor.candidate(FallCandidate(
                key + ':' + str(item['revision']), subject, 'yolo_pose', kind,
                observed, significant_change=item['revision'] > 1, sensors=sensor))
            if iid is not None:
                self._revisions[key] = (item['revision'], item['targetTrackId'])
                self._revisions.move_to_end(key)
                while len(self._revisions) > 1024:
                    self._revisions.popitem(last=False)
                result.append(iid)
        if subjects is not None:
            self.monitor.ingest_subject_frame(SubjectFrame(
                observed_frame, tuple(SubjectPose(
                    f'pose:{self._generation}:{p.subject_key}', p.box, p.state,
                    p.association_usable) for p in subjects), data['subjectCheckMaxGapSec']))
        # Bound per-track timestamp bookkeeping too. IDs are association only.
        if len(self._last) > 1024:
            self._last = {k: v for k, v in self._last.items() if k in {'rgb', 'poses'}}
        return tuple(result)
