"""The VLM in the one fall check (SWM25-261, 2026-10-17): it reports, it does not decide.

With Bringup ``fall_flow`` on, this replaces CloudFallMonitor's judgment. It tells
the Agent's fall rules (fall_rules/fall_flow.py) what it sees, as sightings:
YOLO-Pose candidates, Cloud's periodic finds, someone standing up, and every
two seconds each person still in view. It runs the Cloud looks the rules ask
for (first filter, arrival comparison, what left, posture) and writes their
records and notifications in the journal for the web, as they say. It keeps the
photos those looks need, in memory only: the first sight of each suspicion, the
moment of a fall, and frames of the spot while the robot waits and drives there.

The only filtering left here is a measurement, as in the detector: a Cloud find
whose body rests higher than its floor height (a bed or sofa) is not reported
(2026-10-09). Consent and connection still stop Cloud here; a look asked while
stopped is answered "guard" and the rules ask nothing (unchanged).
"""

import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass, field
import math
import time
from typing import Callable, Optional
from uuid import uuid4

from malbut_agent_server.application.fall_clip_planner import FallClipPlanner, RecordedClip
from malbut_agent_server.application.fall_frame_buffer import FallFrameBuffer
from malbut_agent_server.application.fall_people_recorder import FallPeopleRecorder
from malbut_agent_server.domain.fall_monitoring import (
    ArrivalCheckRequest, CandidateKind, CloudAnalysisExplanation, CloudFallReply,
    CloudFallRequest, DepartureCheckRequest, FallCandidate, FallRuntimePolicy, FrameWindow,
    PersonCheckRequest, PersonObservation, PersonVisibility, PostureCheckRequest, RgbFrame,
    SubjectCheckState, SubjectFrame, VideoAssessment, identifier, timestamp,
)
from malbut_agent_server.application.fall_subject_evidence import FallSubjectEvidence
from malbut_agent_server.ports.cloud_fall import CloudFallProvider, CloudFallProviderError
from malbut_agent_server.ports.fall_event_journal import FallJournalError

# Stopped on purpose (consent, connection, settings): the rules ask nothing then.
GUARD_REASONS = frozenset({
    'waiting_settings', 'disabled', 'camera_off', 'mapping', 'control_unavailable',
    'runtime_error', 'cloud_consent_missing', 'settings_pending', 'server_settings_unavailable',
    'server_settings_stale', 'monitoring_disabled', 'cloud_disconnected',
    'cloud_permission_changed', 'worker_cancelled',
})
RUNTIME_CLOUD_BLOCKS = frozenset({
    'waiting_settings', 'disabled', 'camera_off', 'mapping', 'control_unavailable',
    'runtime_error', 'cloud_consent_missing', 'settings_pending',
    'server_settings_unavailable', 'server_settings_stale',
})
# Each person in view is reported this often, so the rules know who is still there.
SEEN_EVERY_S = 2.0
# The moment of a fall seen by YOLO-Pose: 4 s before to 1 s after, 12 frames (design 5).
MOMENT_BEFORE_S, MOMENT_AFTER_S, MOMENT_FRAMES = 4.0, 1.0, 12
# The current look of the whole body: 3 frames over 2 s (unchanged).
POSTURE_WINDOW_S, POSTURE_FRAMES = 2.0, 3
ARRIVAL_WINDOW_S, ARRIVAL_FRAMES = 1.0, 2
# The spot while the robot waits and drives there: once a second, when it is in
# view, for at most ten minutes per incident (design 6); thinned to stay bounded.
MIDDLE_EVERY_S, MIDDLE_LIMIT_S, MIDDLE_MAX, DEPARTURE_FRAMES = 1.0, 600.0, 120, 12
HFOV_RAD = 1.2915
VIEW_MARGIN = 0.2
VIEW_MAX_M = 8.0
# Photos of closed or never-opened suspicions are dropped after this.
PHOTO_KEEP_S = 1800.0
RECENT_S = 60.0
FURNITURE_HEIGHT_M = 0.30
SUPPORT_BAND = 0.25
LOOKS = ('filter', 'arrival', 'departure', 'posture')
# Which explanation each record may carry (fall_events.json analysisPurposes).
RECORD_PURPOSE = {'analysis_completed': ('incident', 'crosscheck'),
                  'arrival_compared': ('arrival_check',),
                  'departure_reviewed': ('departure_check',),
                  'posture_check_completed': ('posture_check',)}
# 처리 완료 arrives every second while the app lists it; tell the rules now and then.
GUARDIAN_AGAIN_S = 10.0
# The web's assessment shown beside a look's explanation (display only).
SHOWN = {'same_person': VideoAssessment.SUSPECTED_FALL,
         'person_unsure': VideoAssessment.SUSPECTED_FALL,
         'different_person': VideoAssessment.SUSPECTED_FALL,
         'same_object': VideoAssessment.NORMAL_ACTIVITY, 'gone': VideoAssessment.NORMAL_ACTIVITY,
         'not_visible': VideoAssessment.UNOBSERVABLE,
         'animal_left': VideoAssessment.NORMAL_ACTIVITY,
         'person_walked': VideoAssessment.NORMAL_ACTIVITY,
         'person_crawled': VideoAssessment.SUSPECTED_FALL,
         'not_seen': VideoAssessment.UNOBSERVABLE,
         'furniture': VideoAssessment.NORMAL_ACTIVITY, 'standing': VideoAssessment.NORMAL_ACTIVITY,
         'floor_active': VideoAssessment.NORMAL_ACTIVITY,
         'floor': VideoAssessment.SUSPECTED_FALL, 'unclear': VideoAssessment.UNOBSERVABLE}
RECORD_KINDS = frozenset({
    'incident_opened', 'analysis_completed', 'analysis_unavailable', 'approach_started',
    'approach_completed', 'arrival_compared', 'reverify_completed', 'departure_reviewed',
    'view_completed', 'posture_check_completed', 'fall_moment_reviewed', 'question_requested',
    'confirmation_completed', 'voice_result', 'agent_check_failed', 'check_waiting',
    'check_not_confirmed', 'revisit_started', 'check_stopped', 'incident_resolved',
    'recheck_started', 'approach_returned', 'candidate_dropped',
})
STATES = ('verifying', 'recheck_required', 'help_required', 'resolved')
ANSWERS = ('help_request', 'okay', 'unclear', 'no_response', 'failed')


@dataclass(frozen=True)
class Photo:
    frames: tuple
    box: Optional[tuple]
    at: float


@dataclass
class FlowIncident:
    """What the VLM keeps for an incident of the fall rules: photos and its clip."""

    incident_id: str
    boot_id: str
    first: dict
    opened_at: float
    open: bool = True
    check_round: int = 1
    middle: list = field(default_factory=list)
    middle_at: float = -math.inf


@dataclass
class Look:
    look_id: str
    what: str
    sighting_id: Optional[str]
    incident_id: Optional[str]
    photo: Optional[str] = None
    box: Optional[tuple] = None
    track: Optional[str] = None
    fall_moments: tuple = ()


@dataclass(frozen=True)
class AnalysisStatus:
    state: str = 'idle'
    request_id: str = ''
    request_purpose: str = ''
    last_error_code: str = ''


def _point(value):
    return None if value is None else [round(float(value[0]), 3), round(float(value[1]), 3)]


def _box(value):
    if value is None:
        return None
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                       and math.isfinite(v) and 0 <= v <= 1 for v in value)
            or value[0] >= value[2] or value[1] >= value[3]):
        raise ValueError('invalid box')
    return tuple(float(v) for v in value)


def _id(value, *, optional=True):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or len(value) > 200:
        raise ValueError('invalid identifier')
    return value


class FallFlowSensor:
    """One device boot; event-loop confined, with at most one Cloud task."""

    def __init__(self, *, device_id: str, boot_id: str, policy: FallRuntimePolicy,
                 buffer: FallFrameBuffer, provider: CloudFallProvider, journal=None,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time,
                 clip_planner: Optional[FallClipPlanner] = None,
                 people_recorder: Optional[FallPeopleRecorder] = None,
                 place_locator=None, flow_log=None) -> None:
        identifier(device_id)
        identifier(boot_id)
        if provider.execution_target != 'cloud':
            raise ValueError('local VLM providers are not allowed')
        self.device_id, self.boot_id = device_id, boot_id
        self.policy, self.buffer = policy, buffer
        self._provider, self._journal = provider, journal
        self._clock, self._wall_clock = clock, wall_clock
        self._clips = clip_planner or FallClipPlanner()
        self._clip_offsets = {}
        self.clip_storage_failures = 0
        self._people = people_recorder or FallPeopleRecorder(boot_id=boot_id)
        self.people_storage_failures = 0
        self._place = place_locator
        self._flow_log = flow_log  # FallFlowLog: what went to and came from the rules
        self._subjects = FallSubjectEvidence(retention_s=buffer.retention_s,
                                             max_frames=buffer.max_frames)
        self._enabled = self._camera = self._consent = self._connected = False
        self._block = None
        self._epoch = 0
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._analysis = AnalysisStatus()
        self._calls = deque()
        self._scan_anchor = 0.0
        self._last_scan_end = -1.0
        self._person: Optional[PersonObservation] = None
        self._last_person_seen: Optional[float] = None
        self._running_missions = ()
        self._messages = []
        self._looks = deque()
        self._analyses = OrderedDict()   # look_id -> CloudAnalysisExplanation
        self._photos = OrderedDict()     # photo id -> Photo
        self._moments = OrderedDict()    # fall moment id -> Photo (frames only)
        self._pending_moments = {}       # fall moment id -> the fall's time
        self._sightings = OrderedDict()  # sighting id -> (message, evidence start, end)
        self._incidents = {}
        self._seen_at = {}
        self._postures = {}
        self._view = 0
        self._moving = True
        self._storage_failed = False
        self._guardian_sent = OrderedDict()

    # ------------------------------------------------------------ the node's interface

    def _now(self):
        value = self._clock()
        timestamp(value)
        return value

    @property
    def analysis_status(self):
        return self._analysis

    def drain_events(self):
        messages, self._messages = tuple(self._messages), []
        if self._flow_log is not None:
            for message in messages:
                self._flow_log.write('to_rules', message)
        return messages

    def pending_questions(self):
        return ()

    def set_running_missions(self, capability_ids):
        self._running_missions = tuple(capability_ids)

    def configure(self, *, enabled, camera_enabled, cloud_consent, connected):
        if enabled and self._storage_failed:
            raise FallJournalError('recover journal and restart before enabling')
        values = enabled, camera_enabled, cloud_consent, connected
        if any(type(v) is not bool for v in values):
            raise ValueError('settings must be bool')
        previous = self._enabled, self._camera, self._consent, self._connected
        if previous != values:
            self._epoch += 1
        self._enabled, self._camera, self._consent, self._connected = values
        if not all(values):
            self._cancel()
            self._refuse_looks()
        if not enabled or not camera_enabled:
            if self._place is not None:
                self._place.clear()
            self.buffer.clear()
            self._subjects.clear()
            self._people.clear_history()
            self._person = self._last_person_seen = None
            self._seen_at.clear()
            self._postures.clear()
            self._view += 1
        elif not (previous[0] and previous[1]):
            self._scan_anchor = self._now()  # A new periodic interval, not a delayed scan.

    def set_cloud_block(self, reason):
        if reason is not None and reason not in RUNTIME_CLOUD_BLOCKS:
            raise ValueError('invalid runtime Cloud block')
        if reason == self._block:
            return
        self._block = reason
        self._epoch += 1
        if reason is not None:
            self._cancel()
            self._refuse_looks()
        else:
            self._scan_anchor = self._now()

    def ingest_rgb(self, frame: RgbFrame) -> bool:
        if not self._enabled or not self._camera:
            return False
        if frame.captured_at > self._now():
            raise ValueError('future RGB timestamp')
        self.buffer.append(frame)
        return True

    def observe_person(self, observation: PersonObservation) -> bool:
        if not isinstance(observation, PersonObservation):
            raise ValueError('invalid person observation')
        if not self._enabled or not self._camera:
            return False
        previous = self._person
        if previous is not None and observation.observed_at <= previous.observed_at:
            return False
        self._person = observation
        if observation.visibility is PersonVisibility.SEEN:
            self._last_person_seen = observation.observed_at
        return True

    def invalidate_subject_input(self):
        self._subjects.clear()
        self._postures.clear()
        self._view += 1

    def ingest_subject_frame(self, frame: SubjectFrame) -> bool:
        if not isinstance(frame, SubjectFrame):
            raise ValueError('invalid subject frame')
        now = self._now()
        if frame.observed_at > now:
            raise ValueError('future subject frame')
        if (not self._enabled or not self._camera
                or now - frame.observed_at > self.policy.max_person_observation_age_s):
            return False
        self._subjects.append(frame)
        # A box compares only within one view: the view changes whenever the robot moves.
        moving = not frame.camera_stationary
        if moving and not self._moving:
            self._view += 1
        self._moving = moving
        self._people_call(self._people.observe, frame.observed_at, tuple(
            (p.subject_key, p.box, p.strong) for p in frame.subjects if p.box is not None))
        for pose in frame.subjects:
            before = self._postures.get(pose.subject_key)
            self._postures[pose.subject_key] = pose.state
            if pose.box is None:
                continue
            if (before is SubjectCheckState.SUSPECTED and pose.state is SubjectCheckState.CLEAR):
                self._sight('up', frame.observed_at, track=pose.subject_key, box=pose.box)
                self._seen_at[pose.subject_key] = frame.observed_at
            elif (frame.observed_at - self._seen_at.get(pose.subject_key, -math.inf)
                  >= SEEN_EVERY_S):
                self._seen_at[pose.subject_key] = frame.observed_at
                self._sight('seen', frame.observed_at, track=pose.subject_key, box=pose.box)
        while len(self._postures) > 256:
            self._postures.pop(next(iter(self._postures)))
        while len(self._seen_at) > 256:
            self._seen_at.pop(next(iter(self._seen_at)))
        return True

    def candidate(self, candidate: FallCandidate) -> Optional[str]:
        """A YOLO-Pose suspicion, reported as it is; the rules filter it with Cloud."""
        if not isinstance(candidate, FallCandidate):
            raise ValueError('invalid candidate')
        now = self._now()
        if candidate.observed_at > now:
            raise ValueError('future candidate')
        if not self._enabled or not self._camera:
            return None
        if now - candidate.observed_at > self.policy.max_frame_age_s:
            return None
        latest = self._subjects.latest(candidate.subject_key)
        box = latest[2].box if latest is not None else None
        if candidate.kind is CandidateKind.MOTION_SEEN:
            kind = 'motion'
        elif candidate.pose_reason == 'pose_sustained_horizontal_posture':
            kind = 'lying'
        else:
            kind = 'low'
        moment = None
        if kind == 'motion':
            moment = 'm-' + uuid4().hex
            self._pending_moments[moment] = candidate.observed_at
        start = (candidate.evidence_started_at if candidate.evidence_started_at is not None
                 else candidate.observed_at)
        floor = candidate.sensors.floor_distance_m if candidate.sensors is not None else None
        return self._sight(kind, candidate.observed_at, track=candidate.subject_key, box=box,
                           photo=True, fall_moment=moment, evidence=(start, candidate.observed_at),
                           floor=floor)

    def close_by_guardian(self, incident_id):
        """처리 완료 in the app: the rules stop and never check it again."""
        if not isinstance(incident_id, str):
            return
        now = self._now()
        if now - self._guardian_sent.get(incident_id, -math.inf) < GUARDIAN_AGAIN_S:
            return
        if self._incident(incident_id) is not None:
            self._guardian_sent[incident_id] = now
            while len(self._guardian_sent) > 64:
                self._guardian_sent.pop(next(iter(self._guardian_sent)))
            self._messages.append(dict(kind='guardian_closed', incident_id=incident_id))

    def maintain_associations(self):
        """Every loop: finish fall moments, keep frames of spots, drop old photos."""
        now = self._now()
        for moment, at in tuple(self._pending_moments.items()):
            if now >= at + MOMENT_AFTER_S:
                del self._pending_moments[moment]
                frames = self._frames(at + MOMENT_AFTER_S, MOMENT_BEFORE_S + MOMENT_AFTER_S,
                                      MOMENT_FRAMES)
                if frames:
                    self._moments[moment] = Photo(frames, None, at)
        self._collect_middle(now)
        for store in (self._photos, self._moments):
            kept = self._kept_photos()
            for key, photo in tuple(store.items()):
                if now - photo.at > PHOTO_KEEP_S and key not in kept:
                    del store[key]
            while len(store) > 256:
                store.popitem(last=False)
        while (self._sightings
               and now - next(iter(self._sightings.values()))[0]['at'] > PHOTO_KEEP_S):
            self._sightings.popitem(last=False)

    def flush_people(self):
        try:
            ready = self._people.due(self._now())
        except Exception:
            self.people_storage_failures += 1
            return
        for people in ready:
            if self._journal is not None and hasattr(self._journal, 'append_people'):
                try:
                    self._journal.append_people(device_id=self.device_id, people=people)
                except Exception:
                    self.people_storage_failures += 1

    async def close(self):
        self.configure(enabled=False, camera_enabled=False, cloud_consent=False,
                       connected=False)
        if self._task is not None:
            self._task.cancel()

    # ------------------------------------------------------------ decisions of the rules

    def apply(self, data):
        """One decision of the Agent's fall rules (/malbut/falls/runtime/decision)."""
        if not isinstance(data, dict):
            raise ValueError('invalid decision')
        if self._flow_log is not None:
            self._flow_log.write('from_rules', data)
        action = data.get('action')
        if action == 'flow_hello':
            self._ready()
        elif action == 'look':
            what = data.get('what')
            if what not in LOOKS:
                raise ValueError('invalid look')
            look = Look(_id(data.get('look_id'), optional=False), what,
                        _id(data.get('sighting_id')), _id(data.get('incident_id')),
                        _id(data.get('photo') or data.get('before')), _box(data.get('box')),
                        _id(data.get('track')),
                        tuple(_id(m, optional=False) for m in data.get('fall_moments') or ())[:2])
            if (look.sighting_id is None) == (look.incident_id is None):
                raise ValueError('a look is about a sighting or an incident')
            if len(self._looks) >= 32:
                raise ValueError('too many looks')
            if not self._cloud_allowed():
                self._answer(look, 'guard', reason=self._guard_reason())
            else:
                self._looks.append(look)
        elif action == 'record':
            self._write_record(data)
        elif action == 'notify':
            self._write_notice(data)
        elif action == 'link':
            sighting = self._sightings.get(_id(data.get('sighting_id'), optional=False))
            incident = self._incidents.get(_id(data.get('incident_id'), optional=False))
            if sighting is not None and incident is not None:
                self._clip_from(incident, sighting)
        else:
            raise ValueError('unsupported decision')

    def _ready(self):
        """For a restarted Agent: the open incidents and the latest suspicions."""
        open_incidents = []
        stored = self._journal.open_flow_incidents() if self._journal is not None else [
            dict(incident_id=i.incident_id, boot_id=i.boot_id, payload=dict(first=i.first),
                 check_round=i.check_round) for i in self._incidents.values() if i.open]
        for row in stored:
            first = row['payload'].get('first')
            if not isinstance(first, dict):
                continue
            open_incidents.append(dict(incident_id=row['incident_id'], first=first,
                                       check_round=row['check_round'],
                                       fall_moments=row['payload'].get('fall_moments', [])))
        now = self._now()
        recent = [message for message, *_ in self._sightings.values()
                  if now - message['at'] <= RECENT_S
                  and message['kind'] in ('low', 'lying', 'motion', 'cloud')]
        self._messages.append(dict(kind='flow_ready', flow=True,
                                   open_incidents=open_incidents[-64:], recent=recent[-32:]))

    def _incident(self, incident_id):
        incident = self._incidents.get(incident_id)
        if incident is None and self._journal is not None:
            stored = self._journal.flow_incident(incident_id)
            if stored is not None:  # Opened before this process started.
                incident = FlowIncident(incident_id, stored['boot_id'],
                                        stored['payload'].get('first') or {}, self._now(),
                                        stored['open'], stored['check_round'])
                self._incidents[incident_id] = incident
        return incident

    def _write_record(self, data):
        event, incident_id = data.get('event'), data.get('incident_id')
        if event not in RECORD_KINDS:
            raise ValueError('unsupported record')
        if incident_id is None:
            if self._journal is not None:  # Local only (a filtered candidate).
                self._store(self._journal.append_flow_note, dict(
                    event=event, reason=data.get('reason'),
                    sighting_id=data.get('sighting_id')))
            return
        _id(incident_id, optional=False)
        if event == 'incident_opened' and incident_id not in self._incidents:
            sighting = self._sightings.get(data.get('sighting_id'))
            first = sighting[0] if sighting is not None else {}
            incident = FlowIncident(incident_id, self.boot_id, first, self._now())
            self._incidents[incident_id] = incident
            if self._journal is not None:
                self._store(self._journal.save_flow_incident, incident_id=incident_id,
                            boot_id=self.boot_id, payload=dict(first=first, fall_moments=[
                                first.get('fall_moment')] if first.get('fall_moment') else []))
            if sighting is not None:
                self._clip_from(incident, sighting)
        incident = self._incident(incident_id)
        if incident is None:
            raise ValueError('unknown incident')
        analysis = None
        look_id = data.get('look_id')
        if look_id is not None and RECORD_PURPOSE.get(event):
            analysis = self._analyses.get(look_id)
            if analysis is not None and analysis.purpose not in RECORD_PURPOSE[event]:
                analysis = None
        reason = data.get('reason')
        if event == 'approach_started' and reason is None:
            running = self._running_missions
            reason = ('patrol_stopped' if 'patrol' in running
                      else 'follow_stopped' if 'follow_person' in running else None)
        round_ = self._round(data)
        self._append(incident, event, data, reason=reason, analysis=analysis)
        if event == 'incident_resolved':
            incident.open = False
            incident.middle.clear()
            self._mark(incident, round_)
        elif event == 'recheck_started':
            incident.open = True
            incident.middle.clear()
            incident.middle_at = -math.inf
            self._mark(incident, round_)

    def _write_notice(self, data):
        incident = self._incident(_id(data.get('incident_id'), optional=False))
        if incident is None:
            raise ValueError('unknown incident')
        level = data.get('level')
        if level not in ('info', 'check', 'urgent'):
            raise ValueError('invalid level')
        self._append(incident, 'notification_requested', data, reason=data.get('reason'),
                     level=level)

    def _round(self, data):
        value = data.get('check_round', 1)
        if type(value) is not int or not 1 <= value <= 10000:
            raise ValueError('invalid check round')
        return value

    def _append(self, incident, event, data, *, reason, analysis=None, level=None):
        state, answer = data.get('state'), data.get('answer')
        if state not in STATES or (answer is not None and answer not in ANSWERS):
            raise ValueError('invalid record state')
        if reason is not None and (not isinstance(reason, str) or len(reason) > 80):
            raise ValueError('invalid reason')
        fall_seen = data.get('fall_seen') is True
        incident.check_round = self._round(data)
        if self._journal is None:
            return
        self._store(self._journal.append_flow_event, device_id=self.device_id,
                    boot_id=incident.boot_id, event_id=str(uuid4()),
                    incident_id=incident.incident_id, kind=event, state=state,
                    fall_seen=fall_seen,
                    assessment='observed_fall' if fall_seen else 'suspected_fall',
                    answer=answer, reason=reason, notification_level=level,
                    check_round=incident.check_round, analysis=analysis)

    def _mark(self, incident, round_):
        if self._journal is not None:
            self._store(self._journal.mark_flow_incident, incident.incident_id,
                        open=incident.open, check_round=round_)

    def _store(self, method, *args, **kwargs):
        if self._storage_failed:
            raise FallJournalError('recover journal and restart before writing')
        try:
            method(*args, **kwargs)
        except Exception:
            self._storage_failed = True
            self.configure(enabled=False, camera_enabled=False, cloud_consent=False,
                           connected=False)
            raise FallJournalError('fall event persistence failed') from None

    # ------------------------------------------------------------ sightings

    def _sight(self, kind, at, *, track=None, box=None, photo=False, fall_moment=None,
               evidence=None, point=None, frames=None, floor=None):
        sighting_id = 's-' + uuid4().hex
        if point is None and box is not None and self._place is not None:
            try:
                point = self._place.locate_near(at, box)
            except Exception:
                point = None  # Auxiliary: no map place.
        robot = None
        if self._place is not None:
            try:
                camera = self._place.camera_near(at)
            except Exception:
                camera = None
            robot = camera[0] if camera is not None else None
        photo_id = None
        if photo:
            shot = frames or self._frames(at, 1.0, 1)
            if shot:
                photo_id = 'p-' + uuid4().hex
                self._photos[photo_id] = Photo(tuple(shot[-1:]), box, at)
        message = dict(sighting_id=sighting_id, at=at, kind=kind, track=track,
                       point=_point(point), box=list(box) if box is not None else None,
                       view=self._view, robot=_point(robot), on_floor=None, photo=photo_id,
                       fall_moment=fall_moment,
                       # Torso height above the floor by depth (YOLO candidates): for the log.
                       floor=None if floor is None else round(float(floor), 3))
        if kind != 'seen':
            self._sightings[sighting_id] = (message, *(evidence or (at, at)))
            while len(self._sightings) > 512:
                self._sightings.popitem(last=False)
        self._messages.append(dict(kind='fall_sighting', sighting=message))
        return sighting_id

    def _frames(self, end, duration_s, count):
        try:
            return self.buffer.window(
                end=end, duration_s=duration_s, max_images=count,
                max_age_s=max(duration_s, self.policy.max_frame_age_s)).frames
        except ValueError:
            return ()

    def _collect_middle(self, now):
        """Frames of each open spot while it is in view, for "what left" (design 6)."""
        if self._place is None:
            return
        frames = self._frames(now, 1.0, 1)
        if not frames:
            return
        frame = frames[-1]
        try:
            camera = self._place.camera_near(frame.captured_at)
        except Exception:
            camera = None
        if camera is None:
            return
        (cx, cy), (fx, fy) = camera
        for incident in self._incidents.values():
            point = incident.first.get('point')
            if (not incident.open or point is None
                    or now - incident.opened_at > MIDDLE_LIMIT_S
                    or frame.captured_at - incident.middle_at < MIDDLE_EVERY_S):
                continue
            dx, dy = point[0] - cx, point[1] - cy
            distance = math.hypot(dx, dy)
            if distance < 0.2 or distance > VIEW_MAX_M:
                continue
            angle = math.acos(max(-1.0, min(1.0, (dx * fx + dy * fy) / distance)))
            if angle > HFOV_RAD / 2 * (1 - VIEW_MARGIN):
                continue
            incident.middle.append(frame)
            incident.middle_at = frame.captured_at
            if len(incident.middle) > MIDDLE_MAX:
                incident.middle = incident.middle[::2]

    def _kept_photos(self):
        kept = set()
        for incident in self._incidents.values():
            kept.add(incident.first.get('photo'))
            if incident.open:
                kept.add(incident.first.get('fall_moment'))
        return kept

    # ------------------------------------------------------------ clips

    def _clip_from(self, incident, sighting):
        message, start, end = sighting
        anchor = {'motion': 'pose_motion', 'lying': 'pose_found_down', 'low': 'pose_found_down',
                  'cloud': 'cloud_window'}.get(message['kind'])
        if anchor is None:
            return
        self._record_clip(incident, start, end, anchor_kind=anchor,
                          found_down=message['kind'] != 'motion')

    def _record_clip(self, incident, start, end, *, anchor_kind, found_down=False):
        try:
            changed = self._clips.observe(incident.incident_id, start, end,
                                          anchor_kind=anchor_kind, found_down=found_down)
        except ValueError:
            self.clip_storage_failures += 1
            return
        if not changed:
            return
        offset = self._wall_clock() - self._now()
        first = self._clip_offsets.setdefault(incident.incident_id, offset)
        stepped = abs(offset - first) > 1.0
        for segment in changed:
            fixed = self._clip_offsets.setdefault(
                (incident.incident_id, segment.segment_index), offset)
            self._people_call(self._people.segment, incident.incident_id,
                              incident.first.get('track'), segment.segment_index,
                              segment.start, segment.end)
            clip = RecordedClip(
                incident.incident_id, incident.boot_id, segment.segment_index, segment.revision,
                segment.start + fixed, segment.end + fixed, segment.anchor_kinds,
                segment.found_down, stepped)
            if self._journal is not None and hasattr(self._journal, 'append_clip'):
                try:
                    self._journal.append_clip(device_id=self.device_id, clip=clip)
                except Exception:
                    self.clip_storage_failures += 1

    def _people_call(self, method, *args):
        try:
            method(*args)
        except Exception:
            self.people_storage_failures += 1

    # ------------------------------------------------------------ Cloud

    def _cloud_allowed(self):
        return self._guard_reason() is None

    def _guard_reason(self):
        if self._block is not None:
            return self._block
        if not self._enabled or not self._camera:
            return 'monitoring_disabled'
        if not self._consent:
            return 'cloud_consent_missing'
        if not self._connected:
            return 'cloud_disconnected'
        return None

    def _refuse_looks(self):
        reason = self._guard_reason() or 'cloud_permission_changed'
        while self._looks:
            self._answer(self._looks.popleft(), 'guard', reason=reason)

    def _cancel(self):
        if self._task is not None and not self._task.done():
            self._analysis = AnalysisStatus('cancel_requested', self._analysis.request_id,
                                            self._analysis.request_purpose)
            self._task.cancel()

    def _answer(self, look, verdict, *, earlier=None, reason=None):
        message = dict(kind='fall_look_result', look_id=look.look_id, what=look.what,
                       verdict=verdict, earlier=earlier, reason=reason)
        if look.sighting_id is not None:
            message['sighting_id'] = look.sighting_id
        else:
            message['incident_id'] = look.incident_id
        self._messages.append(message)

    def periodic_interval_s(self):
        now = self._now()
        fast = self.policy.scan_interval_s
        if any(i.open for i in self._incidents.values()):
            return fast
        observation = self._person
        if (observation is None
                or now - observation.observed_at > self.policy.max_person_observation_age_s
                or observation.visibility is not PersonVisibility.NOT_SEEN):
            return fast
        if (self._last_person_seen is not None
                and now - self._last_person_seen < self.policy.person_hold_s):
            return fast
        return self.policy.idle_scan_interval_s

    async def run_once(self) -> bool:
        """The next asked look first, then the periodic scan when it is due."""
        if self._block is not None or self._running or (
                self._task is not None and not self._task.done()):
            return False
        self._running = True
        try:
            if not self._enabled or not self._camera:
                return False
            if self._looks:
                look = self._looks.popleft()
                await self._run_look(look)
                return True
            return await self._scan()
        finally:
            self._running = False

    def _rate_limited(self, now):
        while self._calls and self._calls[0] <= now - 60:
            self._calls.popleft()
        return len(self._calls) >= self.policy.max_calls_per_minute

    async def _call(self, request_id, purpose, coroutine):
        """One Cloud call within the timeout; None on any failure (the reason recorded)."""
        epoch = self._epoch
        self._calls.append(self._now())
        self._analysis = AnalysisStatus('waiting_response', request_id, purpose)
        self._task = asyncio.ensure_future(coroutine)
        self._task.add_done_callback(lambda task: task.cancelled() or task.exception())
        try:
            done, _ = await asyncio.wait({self._task}, timeout=self.policy.cloud_timeout_s)
            if self._epoch != epoch or self._task.cancelled():
                self._analysis = AnalysisStatus('canceled', request_id, purpose)
                return None, 'cloud_permission_changed'
            if not done:
                self._task.cancel()
                self._analysis = AnalysisStatus('failed', request_id, purpose, 'cloud_timeout')
                return None, 'cloud_timeout'
            reply = self._task.result()
        except asyncio.CancelledError:
            if self._task is not None:
                self._task.cancel()
            raise
        except CloudFallProviderError as error:
            self._analysis = AnalysisStatus('failed', request_id, purpose, error.code)
            return None, error.code
        except Exception:
            self._analysis = AnalysisStatus('failed', request_id, purpose,
                                            'cloud_failed_or_invalid_response')
            return None, 'cloud_failed_or_invalid_response'
        self._analysis = AnalysisStatus('completed', request_id, purpose)
        return reply, None

    def _explain(self, look_id, request_id, purpose, assessment, explanation):
        try:
            self._analyses[look_id] = CloudAnalysisExplanation(
                request_id, purpose, assessment, explanation)
        except (ValueError, TypeError, UnicodeError):
            return  # Missing display evidence never stops the check.
        while len(self._analyses) > 128:
            self._analyses.popitem(last=False)

    async def _run_look(self, look):
        now = self._now()
        if self._rate_limited(now):
            self._looks.appendleft(look)  # Within the minute's budget: wait, not fail.
            return
        request_id = str(uuid4())
        if look.what == 'filter':
            await self._filter(look, request_id, now)
        elif look.what == 'arrival':
            await self._arrival(look, request_id, now)
        elif look.what == 'departure':
            await self._departure(look, request_id, now)
        else:
            await self._posture(look, request_id, now)

    def _failed(self, look, error):
        if error in GUARD_REASONS:
            self._answer(look, 'guard', reason=error)
        else:
            self._answer(look, 'failed', reason=error)

    async def _filter(self, look, request_id, now):
        """The first look at a suspicion: the Cloud check that used to follow the opening."""
        try:
            window = self.buffer.window(end=now, duration_s=self.policy.clip_window_s,
                                        max_images=self.policy.max_images,
                                        max_age_s=self.policy.max_frame_age_s)
        except ValueError:
            self._answer(look, 'failed', reason='fresh_rgb_unavailable')
            return
        request = CloudFallRequest(
            request_id=request_id, purpose='incident', device_id=self.device_id,
            boot_id=self.boot_id, incident_id=None, subject_key=look.track,
            evidence_revision=1, window=window, sensors=None,
            target=self._subjects.target(look.track, window) if look.track else None)
        reply, error = await self._call(request_id, 'incident', self._provider.analyze(request))
        if reply is None:
            self._failed(look, error)
            return
        if not isinstance(reply, CloudFallReply):
            self._answer(look, 'failed', reason='cloud_failed_or_invalid_response')
            return
        self._explain(look.look_id, request_id, 'incident', reply.assessment, reply.explanation)
        # The shared three questions: not a person, only an image, or ordinary all
        # come back as normal_activity; the rules never drop a fall motion for it.
        self._answer(look, 'ordinary' if reply.assessment is VideoAssessment.NORMAL_ACTIVITY
                     else 'suspect')

    def _photo_window(self, photo_id):
        photo = self._photos.get(photo_id) if photo_id else None
        if photo is None:
            return None, None
        frames = photo.frames
        return FrameWindow(frames, frames[0].captured_at, frames[-1].captured_at, False), photo.box

    def _now_window(self, now, duration_s, count):
        frames = self._frames(now, duration_s, count)
        if not frames:
            return None
        return FrameWindow(frames, frames[0].captured_at, frames[-1].captured_at, False)

    async def _arrival(self, look, request_id, now):
        after = self._now_window(now, ARRIVAL_WINDOW_S, ARRIVAL_FRAMES)
        if after is None:
            self._answer(look, 'failed', reason='fresh_rgb_unavailable')
            return
        before, box = self._photo_window(look.photo)
        if before is None:
            # No first sight (a restart): only whether a person is there now (design 6).
            reply, error = await self._call(
                request_id, 'person_check',
                self._provider.check_person(PersonCheckRequest(request_id, after)))
            if reply is None:
                self._failed(look, error)
                return
            verdict = {'person': 'person_unsure', 'not_person': 'gone'}.get(
                reply.verdict, 'not_visible')
            self._explain(look.look_id, request_id, 'arrival_check', SHOWN[verdict],
                          reply.explanation)
            self._answer(look, verdict)
            return
        reply, error = await self._call(request_id, 'arrival_check', self._provider.check_arrival(
            ArrivalCheckRequest(request_id, before, box or look.box, after)))
        if reply is None:
            self._failed(look, error)
            return
        self._explain(look.look_id, request_id, 'arrival_check', SHOWN[reply.verdict],
                      reply.explanation)
        self._answer(look, reply.verdict)

    async def _departure(self, look, request_id, now):
        incident = self._incidents.get(look.incident_id)
        before, box = self._photo_window(look.photo)
        middle = list(incident.middle) if incident is not None else []
        after = self._now_window(now, 1.0, 1)
        if before is None or not middle or after is None:
            self._answer(look, 'not_seen')  # Nothing to see the leaving in (design 6).
            return
        if len(middle) > DEPARTURE_FRAMES:
            step = (len(middle) - 1) / (DEPARTURE_FRAMES - 1)
            middle = [middle[round(i * step)] for i in range(DEPARTURE_FRAMES)]
        window = FrameWindow(tuple(middle), middle[0].captured_at, middle[-1].captured_at, False)
        reply, error = await self._call(
            request_id, 'departure_check', self._provider.check_departure(
                DepartureCheckRequest(request_id, before, box or look.box, window, after)))
        if reply is None:
            self._failed(look, error)
            return
        self._explain(look.look_id, request_id, 'departure_check', SHOWN[reply.verdict],
                      reply.explanation)
        self._answer(look, reply.verdict)

    async def _posture(self, look, request_id, now):
        window = self._now_window(now, POSTURE_WINDOW_S, POSTURE_FRAMES)
        if window is None:
            self._answer(look, 'failed', reason='fresh_rgb_unavailable')
            return
        moments = tuple(
            FrameWindow(m.frames, m.frames[0].captured_at, m.frames[-1].captured_at, False)
            for m in (self._moments.get(key) for key in look.fall_moments) if m is not None)
        reply, error = await self._call(request_id, 'posture_check', self._provider.check_posture(
            PostureCheckRequest(request_id, window, moments)))
        if reply is None:
            self._failed(look, error)
            return
        self._explain(look.look_id, request_id, 'posture_check', SHOWN[reply.verdict],
                      reply.explanation)
        # Fall moments asked but lost (a restart): the earlier moment is unknown (design 5).
        earlier = reply.earlier if moments else ('unknown' if look.fall_moments else None)
        self._answer(look, reply.verdict, earlier=earlier)

    async def _scan(self):
        """Cloud's periodic look at the whole scene: its finds become sightings."""
        now = self._now()
        if now < self._scan_anchor + self.periodic_interval_s():
            return False
        if self._guard_reason() is not None or self._rate_limited(now):
            self._scan_anchor = now
            return False
        try:
            window = self.buffer.window(end=now, duration_s=self.policy.clip_window_s,
                                        max_images=self.policy.max_images,
                                        max_age_s=self.policy.max_frame_age_s)
        except ValueError:
            return False
        if window.frames[-1].captured_at <= self._last_scan_end:
            return False
        self._scan_anchor = now
        self._last_scan_end = window.frames[-1].captured_at
        request = CloudFallRequest(
            request_id=str(uuid4()), purpose='crosscheck', device_id=self.device_id,
            boot_id=self.boot_id, incident_id=None, subject_key=None, evidence_revision=0,
            window=window, sensors=None)
        reply, _ = await self._call(request.request_id, 'crosscheck',
                                    self._provider.analyze(request))
        if not isinstance(reply, CloudFallReply) or reply.assessment not in (
                VideoAssessment.OBSERVED_FALL, VideoAssessment.SUSPECTED_FALL):
            return True
        times = tuple(f.captured_at for f in window.frames)
        for finding in reply.findings:
            regions = [r for r in finding.regions if r.frame_index < len(times)]
            if self._on_furniture(times, finding, regions):
                continue
            region = regions[-1] if regions else None
            at = times[region.frame_index] if region is not None else times[-1]
            box = region.box if region is not None else None
            point = None
            if box is not None and self._place is not None:
                try:
                    point = self._place.locate(at, box)
                except Exception:
                    point = None
            moment = None
            if finding.assessment is VideoAssessment.OBSERVED_FALL:
                moment = 'm-' + uuid4().hex  # The frames Cloud judged a fall (design 5).
                self._moments[moment] = Photo(window.frames, None, at)
            frame = next((f for f in window.frames if f.captured_at == at), window.frames[-1])
            sighting = self._sight('cloud', at, box=box, point=point, photo=True,
                                   frames=(frame,), fall_moment=moment,
                                   evidence=(times[0], times[-1]))
            # Shown when the rules open an incident on it (look_id = the sighting).
            self._explain(sighting, request.request_id, 'crosscheck', finding.assessment,
                          reply.explanation)
        return True

    def _on_furniture(self, times, finding, regions):
        """Every box rests clearly above the floor and no fall was seen (2026-10-09)."""
        locate3d = getattr(self._place, 'locate3d', None)
        if (locate3d is None or not regions
                or finding.assessment is VideoAssessment.OBSERVED_FALL):
            return False
        heights = []
        for region in regions:
            left, top, right, bottom = region.box
            band = (left, bottom - (bottom - top) * SUPPORT_BAND, right, bottom)
            try:
                spot = locate3d(times[region.frame_index], band)
            except Exception:
                spot = None
            if spot is None:
                return False  # Not measured: a fall cannot be ruled out.
            heights.append(spot[2])
        return min(heights) > FURNITURE_HEIGHT_M
