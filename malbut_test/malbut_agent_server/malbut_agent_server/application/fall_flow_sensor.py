"""The VLM in the one fall check (SWM25-261, 2026-10-17): it reports, it does not decide.

With Bringup ``fall_flow`` on, this replaces CloudFallMonitor's judgment. It tells
the Agent's fall rules (fall_rules/fall_flow.py) what it sees, as sightings:
YOLO-Pose candidates and Cloud's periodic finds. It runs the Cloud looks the rules ask
for (first filter, arrival comparison, what left, posture) and writes their
records and notifications in the journal for the web, as they say. It keeps the
photos those looks need, in memory only: the first sight of each suspicion, the
moment of a fall, and frames of the spot while the robot waits and drives there.

Every YOLO-Pose frame also goes to the Agent as numbers only (``fall_people``,
SWM25-267): each person's YOLO track, box, posture and map place by depth, where
the robot was and whether it stood still. Who is the same person is decided by
the Agent's person tracking, not here (input, judgment and action, 2026-10-19).

The only filtering left here is a measurement, as in the detector: a Cloud find
whose body rests higher than its floor height (a bed or sofa) is not reported
(2026-10-09). Consent and connection still stop Cloud here; a look asked while
stopped is answered "guard" and the rules ask nothing (unchanged).
"""

import asyncio
from collections import OrderedDict, deque
import math
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple
from uuid import uuid4

from malbut_agent_server.application.fall_appearance import FallAppearance
from malbut_agent_server.application.fall_clip_planner import FallClipPlanner
from malbut_agent_server.application.fall_frame_buffer import FallFrameBuffer
from malbut_agent_server.application.fall_people_recorder import FallPeopleRecorder
from malbut_agent_server.application.fall_sensor import (
    ANSWERS,
    ARRIVAL_FRAMES,
    ARRIVAL_WINDOW_S,
    CHECK_CLIP_AFTER_S,
    CHECK_CLIP_EVERY_S,
    CHECK_CLIP_MAX_S,
    CLIP_MARKS,
    COMPARE_WAIT_S,
    DEPARTURE_FRAMES,
    FURNITURE_HEIGHT_M,
    GUARDIAN_AGAIN_S,
    GUARD_REASONS,
    HFOV_RAD,
    LOOKS,
    MIDDLE_EVERY_S,
    MIDDLE_LIMIT_S,
    MIDDLE_MAX,
    MOMENT_AFTER_S,
    MOMENT_BEFORE_S,
    MOMENT_FRAMES,
    PHOTO_KEEP_S,
    POSTURE_FRAMES,
    POSTURE_WINDOW_S,
    RECENT_S,
    RECORD_KINDS,
    RECORD_PURPOSE,
    RUNTIME_CLOUD_BLOCKS,
    SHOWN,
    STATES,
    SUPPORT_BAND,
    VIEW_MARGIN,
    VIEW_MAX_M,
    AnalysisStatus,
    FlowIncident,
    Look,
    Photo,
    _box,
    _id,
    _point,
    cloud_looks,
    journal_ops,
)
from malbut_agent_server.application.fall_subject_evidence import FallSubjectEvidence
from malbut_agent_server.domain.fall_monitoring import (
    CandidateKind,
    FallCandidate,
    FallRuntimePolicy,
    PersonObservation,
    PersonVisibility,
    RgbFrame,
    SubjectFrame,
    identifier,
    timestamp,
)
from malbut_agent_server.ports.cloud_fall import CloudFallProvider
from malbut_agent_server.ports.fall_event_journal import FallJournalError

__all__ = [
    'ANSWERS',
    'ARRIVAL_FRAMES',
    'ARRIVAL_WINDOW_S',
    'AnalysisStatus',
    'CHECK_CLIP_AFTER_S',
    'CHECK_CLIP_EVERY_S',
    'CHECK_CLIP_MAX_S',
    'CLIP_MARKS',
    'COMPARE_WAIT_S',
    'DEPARTURE_FRAMES',
    'FURNITURE_HEIGHT_M',
    'FallFlowSensor',
    'FlowIncident',
    'GUARDIAN_AGAIN_S',
    'GUARD_REASONS',
    'HFOV_RAD',
    'LOOKS',
    'Look',
    'MIDDLE_EVERY_S',
    'MIDDLE_LIMIT_S',
    'MIDDLE_MAX',
    'MOMENT_AFTER_S',
    'MOMENT_BEFORE_S',
    'MOMENT_FRAMES',
    'PHOTO_KEEP_S',
    'POSTURE_FRAMES',
    'POSTURE_WINDOW_S',
    'Photo',
    'RECENT_S',
    'RECORD_KINDS',
    'RECORD_PURPOSE',
    'RUNTIME_CLOUD_BLOCKS',
    'SHOWN',
    'STATES',
    'SUPPORT_BAND',
    'VIEW_MARGIN',
    'VIEW_MAX_M',
    '_box',
    '_id',
    '_point',
]


class FallFlowSensor:
    """One device boot; event-loop confined, with at most one Cloud task."""

    def __init__(
        self,
        *,
        device_id: str,
        boot_id: str,
        policy: FallRuntimePolicy,
        buffer: FallFrameBuffer,
        provider: CloudFallProvider,
        journal: Any = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        clip_planner: Optional[FallClipPlanner] = None,
        people_recorder: Optional[FallPeopleRecorder] = None,
        place_locator: Any = None,
        flow_log: Any = None,
        appearance: Any = None,
    ) -> None:
        identifier(device_id)
        identifier(boot_id)
        if provider.execution_target != 'cloud':
            raise ValueError('local VLM providers are not allowed')
        self.device_id, self.boot_id = device_id, boot_id
        self.policy, self.buffer = policy, buffer
        self._provider, self._journal = provider, journal
        self._clock, self._wall_clock = clock, wall_clock
        self._clips = clip_planner or FallClipPlanner()
        self._clip_offsets: Dict[Any, float] = {}
        self.clip_storage_failures = 0
        self._people = people_recorder or FallPeopleRecorder(boot_id=boot_id)
        self.people_storage_failures = 0
        self._place = place_locator
        self._flow_log = flow_log
        self._appearance = appearance or FallAppearance()
        self._comparisons: 'OrderedDict[str, Tuple[Any, ...]]' = OrderedDict()
        self._subjects = FallSubjectEvidence(
            retention_s=buffer.retention_s, max_frames=buffer.max_frames,
        )
        self._enabled = self._camera = self._consent = self._connected = False
        self._block: Optional[str] = None
        self._epoch = 0
        self._task: Optional[asyncio.Task[Any]] = None
        self._running = False
        self._analysis = AnalysisStatus()
        self._calls: deque[float] = deque()
        self._scan_anchor = 0.0
        self._last_scan_end = -1.0
        self._person: Optional[PersonObservation] = None
        self._last_person_seen: Optional[float] = None
        self._running_missions: Tuple[str, ...] = ()
        self._messages: List[Dict[str, Any]] = []
        self._looks: deque[Look] = deque()
        self._analyses: 'OrderedDict[str, Any]' = OrderedDict()
        self._photos: 'OrderedDict[str, Photo]' = OrderedDict()
        self._moments: 'OrderedDict[str, Photo]' = OrderedDict()
        self._pending_moments: Dict[str, float] = {}
        self._sightings: 'OrderedDict[str, Tuple[Any, ...]]' = OrderedDict()
        self._incidents: Dict[str, FlowIncident] = {}
        self._view = 0
        self._moving = True
        self._storage_failed = False
        self._guardian_sent: 'OrderedDict[str, float]' = OrderedDict()

    # ------------------------------------------------------------ the node's interface

    def _now(self) -> float:
        value = self._clock()
        timestamp(value)
        return value

    @property
    def analysis_status(self) -> AnalysisStatus:
        return self._analysis

    def drain_events(self) -> Tuple[Dict[str, Any], ...]:
        messages, self._messages = tuple(self._messages), []
        if self._flow_log is not None:
            for message in messages:
                self._flow_log.write('to_rules', message)
        return messages

    def pending_questions(self) -> Tuple[Any, ...]:
        return ()

    def set_running_missions(self, capability_ids: Sequence[str]) -> None:
        self._running_missions = tuple(capability_ids)

    def configure(
        self,
        *,
        enabled: bool,
        camera_enabled: bool,
        cloud_consent: bool,
        connected: bool,
    ) -> None:
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
            self._view += 1
        elif not (previous[0] and previous[1]):
            self._scan_anchor = self._now()  # A new periodic interval, not a delayed scan.

    def set_cloud_block(self, reason: Optional[str]) -> None:
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

    def invalidate_subject_input(self) -> None:
        self._subjects.clear()
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
            (p.subject_key, p.box, p.strong) for p in frame.subjects if p.box is not None
        ))
        # Who is still there and who stood up: the Agent's person tracking (SWM25-267).
        self._messages.append(self._people_frame(frame))
        self._describe(frame)
        return True

    def _describe(self, frame: SubjectFrame) -> None:
        """What each person in view looks like, every few seconds (ReID, decision 10)."""
        people = [(p.subject_key, p.box) for p in frame.subjects if p.box is not None]
        self._appearance.see(frame.observed_at, [track for track, _ in people])
        if not any(self._appearance.due(track, frame.observed_at) for track, _ in people):
            return
        shot = self._frames(frame.observed_at, 1.0, 1)
        if shot:
            self._appearance.observe(frame.observed_at, shot[-1].jpeg, people)
        self._answer_comparisons()

    def _appearance_ready(self, after: Any, mode: str) -> bool:
        return (
            self._appearance.ready(after) if mode == 'reappear'
            else self._appearance.ready(after, mode=mode)
        )

    def _appearance_compare(self, before: Any, after: Any, mode: str) -> Optional[float]:
        return (
            self._appearance.compare(before, after) if mode == 'reappear'
            else self._appearance.compare(before, after, mode=mode)
        )

    def _answer_comparisons(self) -> None:
        now = self._now()
        for request_id, (before, after, mode, asked) in list(self._comparisons.items()):
            waiting = (
                not self._appearance_ready(after, mode)
                and (mode == 'reappear' or self._appearance.in_view(after[0], now))
            )
            if waiting and now - asked < COMPARE_WAIT_S:
                continue  # Its second look comes a second after it showed up.
            del self._comparisons[request_id]
            similarity = self._appearance_compare(before, after, mode)
            self._messages.append(dict(
                kind='fall_appearance',
                request_id=request_id,
                similarity=None if similarity is None else round(similarity, 4),
            ))

    def _people_frame(self, frame: SubjectFrame) -> Dict[str, Any]:
        """One frame for the Agent's person tracking; a frame with nobody is sent too."""
        at = frame.observed_at
        robot = None
        if self._place is not None:
            try:
                camera = self._place.camera_near(at)
            except Exception:
                camera = None
            robot = camera[0] if camera is not None else None
        people = []
        for pose in frame.subjects:
            if pose.box is None:
                continue
            people.append(dict(
                track=pose.subject_key,
                box=[round(v, 4) for v in pose.box],
                point=_point(self._pose_place(at, pose)),
                state=pose.state.value,
            ))
        return dict(
            kind='fall_people',
            at=at,
            view=self._view,
            stationary=frame.camera_stationary,
            robot=_point(robot),
            people=people,
        )

    def _pose_place(self, at: float, pose: Any) -> Any:
        if self._place is None:
            return None
        try:
            if pose.torso is not None:
                return self._place.locate_body_near(at, pose.torso)
            return self._place.locate_near(at, pose.box)
        except Exception:
            return None

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
        point = (
            self._pose_place(candidate.observed_at, latest[2])
            if latest is not None and box is not None else None
        )
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
        start = (
            candidate.evidence_started_at
            if candidate.evidence_started_at is not None
            else candidate.observed_at
        )
        floor = candidate.sensors.floor_distance_m if candidate.sensors is not None else None
        return self._sight(
            kind, candidate.observed_at, track=candidate.subject_key, box=box,
            point=point, placed=True, photo=True, fall_moment=moment,
            evidence=(start, candidate.observed_at), floor=floor,
        )

    def close_by_guardian(self, incident_id: Any) -> None:
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

    def maintain_associations(self) -> None:
        """Every loop: finish fall moments, keep frames of spots, drop old photos."""
        now = self._now()
        for moment, at in tuple(self._pending_moments.items()):
            if now >= at + MOMENT_AFTER_S:
                del self._pending_moments[moment]
                frames = self._frames(
                    at + MOMENT_AFTER_S, MOMENT_BEFORE_S + MOMENT_AFTER_S, MOMENT_FRAMES,
                )
                if frames:
                    self._moments[moment] = Photo(frames, None, at)
        self._collect_middle(now)
        self._answer_comparisons()
        for incident in self._incidents.values():
            clip = incident.check_clip
            if clip is None or now - clip[2] < CHECK_CLIP_EVERY_S:
                continue
            if now - clip[1] >= CHECK_CLIP_MAX_S:
                incident.check_clip = None  # A round stuck for ten minutes: stop growing.
            else:
                journal_ops.extend_check(self, incident, now, after=CHECK_CLIP_EVERY_S)
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

    def flush_people(self) -> None:
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

    async def close(self) -> None:
        self.configure(
            enabled=False, camera_enabled=False, cloud_consent=False, connected=False,
        )
        if self._task is not None:
            self._task.cancel()

    # ------------------------------------------------------------ sightings & middle frames

    def _sight(
        self, kind: str, at: float, *, track: Optional[str] = None,
        box: Optional[Tuple[float, float, float, float]] = None,
        photo: bool = False, fall_moment: Optional[str] = None,
        evidence: Optional[Tuple[float, float]] = None,
        point: Any = None, placed: bool = False, frames: Any = None,
        floor: Optional[float] = None,
    ) -> str:
        sighting_id = 's-' + uuid4().hex
        if point is None and not placed and box is not None and self._place is not None:
            try:
                point = self._place.locate_near(at, box)
            except Exception:
                point = None
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
        message = dict(
            sighting_id=sighting_id, at=at, kind=kind, track=track,
            point=_point(point), box=list(box) if box is not None else None,
            view=self._view, robot=_point(robot), on_floor=None, photo=photo_id,
            fall_moment=fall_moment,
            floor=None if floor is None else round(float(floor), 3),
        )
        self._sightings[sighting_id] = (message, *(evidence or (at, at)))
        while len(self._sightings) > 512:
            self._sightings.popitem(last=False)
        self._messages.append(dict(kind='fall_sighting', sighting=message))
        return sighting_id

    def _frames(self, end: float, duration_s: float, count: int) -> Tuple[Any, ...]:
        try:
            return self.buffer.window(
                end=end, duration_s=duration_s, max_images=count,
                max_age_s=max(duration_s, self.policy.max_frame_age_s),
            ).frames
        except ValueError:
            return ()

    def _collect_middle(self, now: float) -> None:
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

    def _kept_photos(self) -> Set[Optional[str]]:
        kept: Set[Optional[str]] = set()
        for incident in self._incidents.values():
            kept.add(incident.first.get('photo'))
            if incident.open:
                kept.add(incident.first.get('fall_moment'))
        return kept

    def _people_call(self, method: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        try:
            method(*args, **kwargs)
        except Exception:
            self.people_storage_failures += 1

    # ------------------------------------------------------------ rule decisions & Cloud

    def apply(self, data: Any) -> None:
        """One decision of the Agent's fall rules (/malbut/falls/runtime/decision)."""
        journal_ops.apply_decision(self, data)

    def _incident(self, incident_id: str) -> Optional[FlowIncident]:
        return journal_ops.get_incident(self, incident_id)

    def _cloud_allowed(self) -> bool:
        return self._guard_reason() is None

    def _guard_reason(self) -> Optional[str]:
        if self._block is not None:
            return self._block
        if not self._enabled or not self._camera:
            return 'monitoring_disabled'
        if not self._consent:
            return 'cloud_consent_missing'
        if not self._connected:
            return 'cloud_disconnected'
        return None

    def _refuse_looks(self) -> None:
        reason = self._guard_reason() or 'cloud_permission_changed'
        while self._looks:
            self._answer(self._looks.popleft(), 'guard', reason=reason)

    def _cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._analysis = AnalysisStatus(
                'cancel_requested',
                self._analysis.request_id,
                self._analysis.request_purpose,
            )
            self._task.cancel()

    def _answer(
        self, look: Look, verdict: str, *,
        earlier: Optional[str] = None, reason: Optional[str] = None,
    ) -> None:
        message: Dict[str, Any] = dict(
            kind='fall_look_result', look_id=look.look_id, what=look.what,
            verdict=verdict, earlier=earlier, reason=reason,
        )
        if look.sighting_id is not None:
            message['sighting_id'] = look.sighting_id
        else:
            message['incident_id'] = look.incident_id
        self._messages.append(message)

    def periodic_interval_s(self) -> float:
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
                await cloud_looks.run_look(self, look)
                return True
            return await cloud_looks.run_scan(self)
        finally:
            self._running = False

    def _rate_limited(self, now: float) -> bool:
        while self._calls and self._calls[0] <= now - 60:
            self._calls.popleft()
        return len(self._calls) >= self.policy.max_calls_per_minute

    def _provider_blocked(self) -> Optional[str]:
        probe = getattr(self._provider, 'blocked_reason', None)
        return probe() if callable(probe) else None
