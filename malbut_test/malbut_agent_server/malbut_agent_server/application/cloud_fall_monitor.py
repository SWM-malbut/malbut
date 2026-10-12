"""Cloud-only fall workflow core, driven by an owning asyncio event loop.

Adapters ingest RGB/candidates and deliver events to speech/notification sinks.
This module never infers a spoken answer, drives the robot, sends a guardian
message, or imports the evaluation runner. Only the approved narrow normal
closure rule is automatic; other closure decisions remain explicit.
"""

import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass, replace
import math
import time
from typing import Callable, Optional
from uuid import uuid4

from malbut_agent_server.application.fall_clip_planner import FallClipPlanner, RecordedClip
from malbut_agent_server.application.fall_frame_buffer import FallFrameBuffer
from malbut_agent_server.application.fall_people_recorder import FallPeopleRecorder
from malbut_agent_server.application.fall_normal_closure import (
    normal_closure_supported, normal_path_ready,
)
from malbut_agent_server.application.fall_scene_place import (
    CASE_GAP_S, SAME_PLACE_M, CameraMotionLog, near_on_map, same_place,
)
from malbut_agent_server.application.fall_subject_evidence import FallSubjectEvidence
from malbut_agent_server.application.fall_pending_association import AssociationWait, PendingAssociation
from malbut_agent_server.application.fall_cloud_association import (
    associate_timed_finding, box_iou, supplement_samples, timed_association_evidence,
)
from malbut_agent_server.application.fall_deferred_association import (
    DeferredDiscovery, DiscoveryLinkResult, DiscoveryTrack,
)
from malbut_agent_server.domain.fall_monitoring import (
    AgentCheckReply, CloudFallReply, CloudFallRequest, FallCandidate, FallIncident,
    FallRuntimeEvent, FallRuntimePolicy, IncidentState, RgbFrame,
    NotificationLevel, PersonObservation, PersonVisibility,
    NormalVideoCheck, SubjectCheckState, SubjectObservation, SubjectFrame,
    VideoAssessment, VoiceAnswer, identifier, timestamp,
    CandidateKind, CloudDiscovery, CloudDiscoveryLink, CloudPersonFinding, CloudPoseLink,
    CloudAssociationReview, CloudAnalysisExplanation, PersonCheckReply, PersonCheckRequest,
    PostureCheckReply, PostureCheckRequest, SamePersonCheckReply, SamePersonCheckRequest,
)
from malbut_agent_server.ports.cloud_fall import CloudFallProvider, CloudFallProviderError
from malbut_agent_server.ports.fall_event_journal import FallEventJournal, FallJournalError

RUNTIME_CLOUD_BLOCKS = frozenset({
    'waiting_settings', 'disabled', 'camera_off', 'mapping', 'control_unavailable',
    'runtime_error', 'cloud_consent_missing', 'settings_pending',
    'server_settings_unavailable', 'server_settings_stale',
})
# Consent, connection and settings stop Cloud on purpose: such a failure is
# neither sent again nor turned into a question.
GUARD_FAILURES = RUNTIME_CLOUD_BLOCKS | {
    'monitoring_disabled', 'cloud_disconnected', 'cloud_permission_changed', 'worker_cancelled',
}


APPROACH_OUTCOMES = frozenset({'arrived', 'no_map', 'no_path', 'timeout', 'failed', 'rejected'})
# After arriving 1 m in front: Pose looks this long, then Cloud once.
PERSON_POSE_WAIT_S = 3.0
# While the robot drives to a spot, looks and drives back, that spot belongs to
# the incident being checked (2026-10-09): from 1 m, Pose read the bag pile it
# had just been sent to look at as a person lying down and opened a second
# incident. A check normally ends with its result or return; this bounds it.
SPOT_CHECK_MAX_S = 240.0
# A body resting higher than this is on a bed or sofa, not on the floor: the
# 30 cm the Pose detector already applies to the torso (maximum_floor_height_m).
# 2026-10-09: a person lying on the bed became a Cloud scene case every minute.
FURNITURE_HEIGHT_M = 0.30
# The lowest quarter of a Cloud box is what the body rests on; its centre would
# rise to 30-40 cm for someone slumped against a wall on the floor.
SUPPORT_BAND = 0.25
# fall_full_view (2026-10-11): the 12 cm camera showed only the legs of someone
# sitting on a bed, so a close look said "person" and the robot asked. When the
# look finds a person, the robot moves back to where the whole body is visible
# (at most VIEW_LIMIT_S, the user's one minute) and asks Cloud where the body
# rests, twice POSTURE_GAP_S apart on new frames. It closes without asking only
# when both looks say furniture or standing; anything else asks. Pose keypoints
# are not used for this: from 12 cm a seated person's thighs hide the hips.
VIEW_LIMIT_S = 60.0
POSTURE_SETTLE_S = 1.0
POSTURE_GAP_S = 2.5
OFF_FLOOR_LOOKS = frozenset({'furniture', 'standing'})
OFF_FLOOR_REASONS = frozenset({'sitting_off_floor', 'standing'})
# The robot could not ask for a minute (2026-10-12): it goes back to the spot and
# looks again, but never closes the case itself; the guardian does. What it saw
# goes to the guardian, compared by clothing with the first scene for reference.
WAITING_REASONS = frozenset({'manual_drive', 'busy'})
NOT_CONFIRMED_REASONS = frozenset({'waited_manual_drive', 'waited_busy'})
CHECK_FAILURE_REASONS = frozenset({'speech_unavailable', 'question_error', 'time_limit'})
# A comparison not done by then (Cloud off meanwhile) is recorded as unclear.
COMPARE_WAIT_S = 60.0
FIRST_SCENES_MAX = 16
# One person can come out of YOLO-pose as several tracks (2026-10-10 18:44: one
# person lying on the floor became three tracks and three incidents). Tracks seen
# together were one person at IoU 0.54-0.82; another person's box was at most
# 0.12. A track appearing 2.2 s after another vanished sat 87% inside its last
# box. So open person incidents never outnumber the people in view.
SAME_PERSON_IOU = 0.5
SAME_PERSON_INSIDE = 0.8
SAME_PERSON_GAP_S = 5.0


def _inside(a, b):
    """How much of the smaller box lies inside the other."""
    area = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(
        0, min(a[3], b[3]) - max(a[1], b[1]))
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return area / smaller if smaller else 0.0


@dataclass
class PersonCheck:
    """Looking from close range after the robot drove near an uncertain suspicion."""

    question_id: str
    target: tuple
    started: float
    pose_deadline: float
    cloud: bool = False
    asked: bool = False
    # 'person' (1 m look), 'view' (moving to see the whole body) or 'posture'.
    stage: str = 'person'
    deadline: float = 0.0
    looks: tuple = ()  # Cloud posture verdicts so far
    # Back at the spot after waiting: report what is seen, never close the case.
    revisit: bool = False


@dataclass(frozen=True)
class CloudAnalysisStatus:
    state: str = 'idle'
    request_id: str = ''
    request_purpose: str = ''
    last_error_code: str = ''


class CloudFallMonitor:
    """One device boot; event-loop confined, with at most one Cloud task.

    All settings begin OFF, including consent. Snapshot access returns copies.
    Without a journal, events are only an in-memory test handoff. With a journal,
    incident metadata is committed before exposure; upload runs separately.
    """

    def __init__(self, *, device_id: str, boot_id: str,
                 policy: FallRuntimePolicy, buffer: FallFrameBuffer,
                 provider: CloudFallProvider,
                 journal: Optional[FallEventJournal] = None,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time,
                 clip_planner: Optional[FallClipPlanner] = None,
                 people_recorder: Optional[FallPeopleRecorder] = None,
                 place_locator=None,
                 place_log: Optional[Callable[[str], None]] = None) -> None:
        identifier(device_id)
        identifier(boot_id)
        if provider.execution_target != 'cloud':
            raise ValueError('local VLM providers are not allowed')
        self.device_id, self.boot_id = device_id, boot_id
        # Heights of suspected spots above the floor, for choosing a bed/sofa
        # threshold from real numbers before any decision uses them (2026-10-09).
        self._place_log = place_log
        self.policy, self.buffer = policy, buffer
        self._provider, self._clock = provider, clock
        self._journal = journal
        self._storage_failed = False
        # Clip ranges are auxiliary: a clip failure must never stop fall handling.
        self._wall_clock = wall_clock
        self._clips = clip_planner or FallClipPlanner()
        self._clip_offsets = {}
        # Test/diagnostic handoff only; production never drains, so keep it bounded.
        self._clip_ranges = deque(maxlen=256)
        self.clip_storage_failures = 0
        # Person boxes for the clip overlay: auxiliary like clip ranges.
        self._people = people_recorder or FallPeopleRecorder(boot_id=boot_id)
        self._people_ready = deque(maxlen=256)
        self.people_storage_failures = 0
        self._enabled = self._camera = self._consent = self._connected = False
        self._epoch = 0
        self._incidents = {}
        # One immutable question context per incident; live evidence may advance.
        self._questions = {}
        # A completed conversation may clear an incident while the person is
        # still lying down. Retain only continuously measured SAME-target
        # episodes; missing/ambiguous Pose must never clear or identify people.
        self._settled_subjects = {}
        self._events = []
        self._calls = deque()
        self._task: Optional[asyncio.Task] = None
        self._active_incident: Optional[str] = None
        self._running = False
        self._runtime_cloud_block = None
        self._analysis_status = CloudAnalysisStatus()
        self._scan_anchor = 0.0
        self._person_observation: Optional[PersonObservation] = None
        self._last_person_seen: Optional[float] = None
        self._last_scan_end = -1.0
        self._subject_evidence = FallSubjectEvidence(
            retention_s=buffer.retention_s, max_frames=buffer.max_frames)
        # Bounded metadata only. Tracking is opt-in via the local adapter
        # boundary below; it is not enabled by a Cloud response or ROS command.
        self._discoveries = OrderedDict()
        self._association_wait = AssociationWait()
        # A scene case is one continuing situation: same place in the home,
        # no long silence. Map points come from the optional locator (AMCL +
        # depth); image positions are the fallback. Bounded so active scene
        # cases never take the person-case capacity.
        self._place = place_locator
        self._camera_motion = CameraMotionLog()
        self.max_open_scene_cases = 3
        # Drive near uncertain suspicions before asking (fall_approach). Off
        # until verified on the robot; the ROS node sets it from a parameter.
        self.approach_enabled = False
        # Look at the whole body before asking (fall_full_view). Off until the
        # robot measurement is checked on the robot; the ROS node sets these.
        self.full_view_enabled = False
        self._running_missions = ()
        self._person_checks = {}
        # incident_id -> (map point, since): spots the robot is checking now.
        self._spot_checks = {}
        # Notifications (2026-10-12): the older reasons send only a higher level
        # than they sent before; the new ones (could not check, revisit results)
        # go once each, so a later 도움 요청 is never swallowed.
        self._legacy_levels = {}
        self._sent_notices = {}
        # incident_id -> (first frames, the person's box): the scene that raised
        # the question, in memory only, for the clothing comparison after going back.
        self._first_scenes = OrderedDict()
        self._comparisons = deque()
        # 처리 완료 arrived while the incident's analysis was in flight: closed after it.
        self._guardian_closing = set()
        # Pose track -> open incident of the same person under another track.
        self._same_person = {}

    def _now(self) -> float:
        value = self._clock()
        timestamp(value)
        return value

    def _emit(self, kind: str, incident: Optional[FallIncident] = None,
              **kwargs) -> None:
        if self._storage_failed:
            raise FallJournalError('recover journal and restart monitor before emitting')
        event = FallRuntimeEvent(
            event_id=str(uuid4()), kind=kind,
            incident_id=incident.incident_id if incident else None,
            subject_key=incident.subject_key if incident else None,
            confirmation_scope=('scene' if incident and incident.subject_key is None
                                else 'subject'),
            evidence_revision=incident.revision if incident else None, **kwargs)
        if self._journal is not None and (incident is not None or event.discovery is not None):
            try:
                if event.discovery is not None:
                    self._journal.append_discovery(
                        device_id=self.device_id, boot_id=self.boot_id, event=event)
                else:
                    self._journal.append(device_id=self.device_id, boot_id=self.boot_id,
                                         event=event, incident=replace(incident))
            except Exception:
                self._storage_failed = True
                self.configure(enabled=False, camera_enabled=False,
                               cloud_consent=False, connected=False)
                raise FallJournalError('fall event persistence failed') from None
        self._events.append(event)

    def drain_events(self):
        """Caller must persist/route these; this is not a delivery receipt."""
        events, self._events = tuple(self._events), []
        return events

    def drain_clip_ranges(self):
        """Recorded clip ranges since the last drain; not an upload receipt."""
        ranges = tuple(self._clip_ranges)
        self._clip_ranges.clear()
        return ranges

    def drain_people(self):
        """Finalized person boxes since the last drain; not an upload receipt."""
        ready = tuple(self._people_ready)
        self._people_ready.clear()
        return ready

    def _people_call(self, method, *args) -> None:
        try:
            method(*args)
        except Exception:
            self.people_storage_failures += 1

    def flush_people(self) -> None:
        """Persist segments whose boxes are complete; never stops fall handling."""
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
                    continue
            self._people_ready.append(people)

    def _record_cloud_boxes(self, incident: FallIncident, times, finding) -> None:
        for region in finding.regions:
            if region.frame_index < len(times):
                self._people_call(self._people.cloud, incident.incident_id,
                                  times[region.frame_index], region.box)

    def _record_clip(self, incident: FallIncident, start: float, end: float, *,
                     anchor_kind: str, found_down: bool = False) -> None:
        """Map an evidence window (monotonic) to wall-clock recording ranges."""
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
            # Each segment keeps the offset it was first recorded with: frames
            # archived before a clock step stay at their original wall time.
            fixed = self._clip_offsets.setdefault(
                (incident.incident_id, segment.segment_index), offset)
            self._people_call(self._people.segment, incident.incident_id, incident.subject_key,
                              segment.segment_index, segment.start, segment.end)
            clip = RecordedClip(
                incident.incident_id, self.boot_id, segment.segment_index, segment.revision,
                segment.start + fixed, segment.end + fixed, segment.anchor_kinds,
                segment.found_down, stepped)
            if self._journal is not None and hasattr(self._journal, 'append_clip'):
                try:
                    self._journal.append_clip(device_id=self.device_id, clip=clip)
                except Exception:
                    self.clip_storage_failures += 1
                    continue
            self._clip_ranges.append(clip)

    def incident(self, incident_id: str) -> FallIncident:
        return replace(self._incidents[incident_id])

    @property
    def analysis_status(self):
        if (self._analysis_status.state == 'cancel_requested'
                and self._task is not None and self._task.done()):
            self._analysis_status = replace(self._analysis_status, state='canceled')
        return self._analysis_status

    def _cancel_analysis(self):
        if self._task is not None and not self._task.done():
            self._analysis_status = replace(
                self._analysis_status, state='cancel_requested', last_error_code='')
            self._task.cancel()

    def set_cloud_block(self, reason):
        """Suspend transmission, retaining incidents but not an offline request queue."""
        if reason is not None and reason not in RUNTIME_CLOUD_BLOCKS:
            raise ValueError('invalid runtime Cloud block')
        if reason == self._runtime_cloud_block:
            return
        self._runtime_cloud_block = reason
        self._epoch += 1
        self._cancel_pending_associations()
        if reason is not None:
            self._cancel_analysis()
            for incident in self._incidents.values():
                if incident.pending:
                    incident.pending = False
                    self._failure(incident, reason)
        else:
            # Recovery starts a new periodic interval, not a delayed upload.
            self._scan_anchor = self._now()

    def configure(self, *, enabled: bool, camera_enabled: bool,
                  cloud_consent: bool, connected: bool) -> None:
        if enabled and self._storage_failed:
            raise FallJournalError('recover journal and restart monitor before enabling')
        values = enabled, camera_enabled, cloud_consent, connected
        if any(type(v) is not bool for v in values):
            raise ValueError('settings must be bool')
        previous = self._enabled, self._camera, self._consent, self._connected
        if previous != values:
            self._epoch += 1
            self._cancel_pending_associations()
        self._enabled, self._camera, self._consent, self._connected = values
        if not all(values):
            self._cancel_analysis()
        if not enabled or not camera_enabled:
            self._camera_motion.unknown(self._now())
            if self._place is not None:
                self._place.clear()
            self.buffer.clear()
            self._subject_evidence.clear()
            self._settled_subjects.clear()
            self._same_person.clear()
            self._people.clear_history()
            self._discoveries.clear()
            self._person_observation = None
            self._last_person_seen = None
            for incident in self._incidents.values():
                incident.normal_checks = ()
                incident.subject_observation = None
        if enabled and camera_enabled and not (previous[0] and previous[1]):
            self._scan_anchor = self._now()
        # Consent removal does not disable YOLO or resolve an open incident.

    def ingest_rgb(self, frame: RgbFrame) -> bool:
        if not self._enabled or not self._camera:
            return False
        if frame.captured_at > self._now():
            raise ValueError('future RGB timestamp')
        self.buffer.append(frame)
        return True

    def observe_person(self, observation: PersonObservation) -> bool:
        """Healthy negative detections reduce frequency, never disable scanning."""
        if not isinstance(observation, PersonObservation):
            raise ValueError('invalid person observation')
        now = self._now()
        if observation.observed_at > now:
            raise ValueError('future person observation')
        if not self._enabled or not self._camera:
            return False
        if now - observation.observed_at > self.policy.max_person_observation_age_s:
            return False
        previous = self._person_observation
        if previous is not None and observation.observed_at <= previous.observed_at:
            return False
        self._person_observation = observation
        if observation.visibility is PersonVisibility.SEEN:
            self._last_person_seen = observation.observed_at
        else:
            # Losing detector health/visibility after a subject check cannot
            # leave that check usable for a later automatic closure.
            for incident in self._incidents.values():
                subject = incident.subject_observation
                if subject is not None and observation.observed_at >= subject.observed_at:
                    incident.subject_observation = None
        return True

    def periodic_interval_s(self) -> float:
        """Missing/stale/failed detector input is unknown, not an empty room."""
        now = self._now()
        observation = self._person_observation
        fast = self.policy.scan_interval_s
        if any(i.state is not IncidentState.RESOLVED for i in self._incidents.values()):
            return fast
        if (observation is None
                or now - observation.observed_at > self.policy.max_person_observation_age_s
                or observation.visibility is not PersonVisibility.NOT_SEEN):
            return fast
        if (self._last_person_seen is not None
                and now - self._last_person_seen < self.policy.person_hold_s):
            return fast
        return self.policy.idle_scan_interval_s

    def observe_subject(self, observation: SubjectObservation) -> bool:
        """Explicit association/observation boundary, not scene-level presence.

        There is deliberately no conversion from empty candidate arrays into
        CLEAR. The supplying detector/association module must verify it.
        """
        if not isinstance(observation, SubjectObservation):
            raise ValueError('invalid subject observation')
        now = self._now()
        if observation.observed_at > now:
            raise ValueError('future subject observation')
        incident = self._incidents.get(observation.incident_id)
        if (not self._enabled or not self._camera or incident is None
                or incident.state is IncidentState.RESOLVED
                or observation.subject_key != incident.subject_key
                or observation.evidence_revision != incident.revision
                or not incident.normal_checks
                or observation.request_id != incident.normal_checks[-1].request_id
                or now - observation.observed_at > self.policy.max_person_observation_age_s
                or observation.observed_at < incident.normal_checks[-1].window_end):
            return False
        previous = incident.subject_observation
        if previous is not None and observation.observed_at <= previous.observed_at:
            return False
        incident.subject_observation = observation
        if (observation.state is not SubjectCheckState.CLEAR
                or not observation.association_verified):
            incident.normal_checks = ()
            incident.normal_evidence_after = observation.observed_at
        self._decision_needed(incident)
        return True

    def invalidate_subject_input(self):
        self._camera_motion.unknown(self._now())
        self._subject_evidence.active = True
        self._subject_evidence.clear()
        self._settled_subjects.clear()
        self._same_person.clear()
        self._association_wait.anchors.clear()
        self._association_wait.stationary_at = None
        for entry in self._discoveries.values():
            if entry.track is not None:
                entry.track.broken = True
        for incident in self._incidents.values():
            incident.subject_observation = None

    def ingest_subject_frame(self, frame: SubjectFrame) -> bool:
        if not isinstance(frame, SubjectFrame):
            raise ValueError('invalid subject frame')
        now = self._now()
        if frame.observed_at > now:
            raise ValueError('future subject frame')
        if (not self._enabled or not self._camera
                or now - frame.observed_at > self.policy.max_person_observation_age_s):
            return False
        self._subject_evidence.append(frame)
        self._camera_motion.observe(frame.observed_at, moving=frame.camera_moving)
        self._refresh_settled_subjects()
        self._people_call(self._people.observe, frame.observed_at, tuple(
            (p.subject_key, p.box, p.strong) for p in frame.subjects if p.box is not None))
        for incident in self._incidents.values():
            if incident.state is IncidentState.RESOLVED:
                continue
            if incident.subject_association_token is None:
                incident.subject_association_token = self._subject_evidence.token_at(
                    incident.subject_key, incident.last_observed_at)
            self._refresh_subject(incident)
            # Do not emit a decision event for every camera frame.
            if normal_closure_supported(
                    incident, now=now,
                    max_observation_age_s=self.policy.max_person_observation_age_s):
                self._decision_needed(incident)
        self._retry_pose_associations()
        self._association_wait.observe(frame, self._incidents, self._subject_evidence)
        self._retry_pending_associations()
        return True

    def _remember_settled_subject(self, incident):
        """Guard user-cleared, still-low posture, not a timer-based cooldown."""
        if (incident.subject_key is None or incident.state is not IncidentState.RESOLVED
                or incident.answer is not VoiceAnswer.OKAY):
            return
        latest = self._subject_evidence.latest(incident.subject_key)
        if (latest is None or not latest[1]
                or latest[1] != incident.subject_association_token
                or self._now() - latest[0] > self.policy.max_person_observation_age_s
                or latest[2].state is not SubjectCheckState.SUSPECTED):
            return
        self._settled_subjects[incident.subject_key] = (latest[1], incident.incident_id)

    def _refresh_settled_subjects(self):
        for key, (token, _) in tuple(self._settled_subjects.items()):
            latest = self._subject_evidence.latest(key)
            if (latest is None or latest[1] != token
                    or latest[2].state is SubjectCheckState.CLEAR):
                # CLEAR is the detector's sustained upright observation, not
                # merely a missing box or a scene-wide normal VLM result.
                del self._settled_subjects[key]

    def _settled_subject(self, key, token, finding):
        saved = self._settled_subjects.get(key)
        if (saved is None or saved[0] != token
                or finding.kind is not CandidateKind.ALREADY_DOWN
                or finding.assessment is not VideoAssessment.SUSPECTED_FALL):
            return None
        latest = self._subject_evidence.latest(key)
        if (latest is None or latest[1] != token
                or self._now() - latest[0] > self.policy.max_person_observation_age_s
                or latest[2].state is not SubjectCheckState.SUSPECTED):
            return None
        incident = self._incidents[saved[1]]
        return incident if incident.state is IncidentState.RESOLVED else None

    def _refresh_subject(self, incident):
        if not self._subject_evidence.active:
            return  # Explicit trusted observation boundary remains available.
        incident.subject_observation = None
        checks = incident.normal_checks
        latest = self._subject_evidence.latest(incident.subject_key)
        if not checks or latest is None:
            return
        stamp, token, pose = latest
        if stamp < checks[-1].window_end:
            return
        if pose.state is SubjectCheckState.SUSPECTED:
            incident.normal_checks = ()
            incident.normal_evidence_after = stamp
            return
        if (len(checks) != 2 or not token
                or any(c.target_token != token for c in checks)
                or pose.state is not SubjectCheckState.CLEAR):
            return
        incident.subject_observation = SubjectObservation(
            incident.incident_id, incident.subject_key, incident.revision,
            checks[-1].request_id, stamp, pose.state, True)

    def candidate(self, candidate: FallCandidate) -> Optional[str]:
        """Subject association is the caller's job, never a VLM identity."""
        if not isinstance(candidate, FallCandidate):
            raise ValueError('invalid candidate')
        now = self._now()
        if candidate.observed_at > now:
            raise ValueError('future candidate')
        if not self._enabled or not self._camera:
            return None
        if now - candidate.observed_at > self.policy.max_frame_age_s:
            self._emit('candidate_rejected', reason='stale_candidate')
            return None
        display_reason = (candidate.pose_reason or 'pose_candidate'
                          if candidate.source == 'yolo_pose' else 'cloud_crosscheck')
        if candidate.kind is CandidateKind.MOTION_SEEN:
            # A newly measured fall must not inherit an earlier resting answer.
            self._settled_subjects.pop(candidate.subject_key, None)
        current = next((i for i in self._incidents.values()
                        if i.subject_key == candidate.subject_key
                        and i.state is not IncidentState.RESOLVED), None)
        if current is None:
            current = self._same_person_incident(candidate)
        if current is not None:
            if candidate.source not in current.candidate_sources:
                current.candidate_sources += (candidate.source,)
                current.normal_checks = ()
                current.subject_observation = None
            if (candidate.candidate_id == current.last_candidate_id
                    or candidate.observed_at <= current.last_observed_at):
                return current.incident_id
            current.last_candidate_id = candidate.candidate_id
            current.last_observed_at = candidate.observed_at
            current.sensors = candidate.sensors
            self._record_candidate_clip(current, candidate)
            # Any fresh suspicion invalidates earlier clearance, even if it
            # does not warrant a new question/evidence revision.
            current.normal_checks = ()
            current.subject_observation = None
            current.normal_evidence_after = candidate.observed_at
            if candidate.significant_change:
                current.revision += 1
                current.kind = candidate.kind
                self.request_recheck(current.incident_id)
                # Old answers cannot describe the newly observed change.
                current.question_id = self._pending_question_id(current)
                current.answer = None
                current.situation_assessment = None
                current.help_needed = None
                self._emit('incident_updated', current, reason=display_reason)
            return current.incident_id
        if (candidate.kind is not CandidateKind.MOTION_SEEN
                and self._spot_under_check(candidate.subject_key) is not None):
            # Its close look decides. The detector sends the candidate again,
            # so a person still there after the check opens an incident then.
            self._emit('candidate_rejected', reason='spot_under_check')
            return None
        if not self._make_room():
            self._emit('candidate_rejected', reason='incident_capacity')
            return None
        current = FallIncident(
            incident_id=str(uuid4()), subject_key=candidate.subject_key,
            kind=candidate.kind, opened_at=now,
            last_observed_at=candidate.observed_at,
            last_candidate_id=candidate.candidate_id, sensors=candidate.sensors,
            candidate_sources=(candidate.source,),
            normal_evidence_after=candidate.observed_at,
            subject_association_token=self._subject_evidence.token_at(
                candidate.subject_key, candidate.observed_at))
        self._incidents[current.incident_id] = current
        self._emit('incident_opened', current, reason=display_reason)
        self._log_pose_place(current)
        self._record_candidate_clip(current, candidate)
        if self._runtime_cloud_block is not None:
            current.pending = False
            self._failure(current, self._runtime_cloud_block)
        return current.incident_id

    def _record_window_clip(self, incident: FallIncident, request, finding) -> None:
        # The analyzed window, not the reply arrival time; the motion inside it
        # cannot be pinned down further, so the whole window is kept.
        frames = request.window.frames
        self._record_clip(incident, frames[0].captured_at, frames[-1].captured_at,
                          anchor_kind='cloud_window',
                          found_down=finding.kind is CandidateKind.ALREADY_DOWN)
        self._record_cloud_boxes(incident, tuple(f.captured_at for f in frames), finding)

    def _record_candidate_clip(self, incident: FallIncident, candidate: FallCandidate) -> None:
        if candidate.kind is CandidateKind.ALREADY_DOWN:
            # The fall itself happened before discovery; anchor on the discovery.
            self._record_clip(incident, candidate.observed_at, candidate.observed_at,
                              anchor_kind='pose_found_down')
            return
        start = (candidate.evidence_started_at if candidate.evidence_started_at is not None
                 else candidate.observed_at)
        self._record_clip(incident, start, candidate.observed_at, anchor_kind='pose_motion')

    def _pending_question_id(self, incident):
        question = self._questions.get(incident.incident_id)
        return question.question_id if question is not None and question.answer is None else None

    def ask_question(self, incident_id: str) -> Optional[str]:
        incident = self._incidents[incident_id]
        if incident.state is IncidentState.RESOLVED:
            return None
        if incident.question_id is not None and incident.answer is None:
            return incident.question_id
        incident.question_id = str(uuid4())
        incident.answer = None
        incident.answer_question_played = False
        incident.situation_assessment = None
        incident.help_needed = None
        incident.approach_target = self._approach_target(incident)
        incident.revisit_target = (incident.approach_target if incident.approach_target is not None
                                   else self._spot(incident))
        if incident.approach_target is not None:
            self._spot_checks[incident_id] = (incident.approach_target, self._now())
        self._remember_first_scene(incident)
        if incident.video is not None:
            self._questions[incident_id] = replace(incident)
        self._emit('question_requested', incident,
                   question_id=incident.question_id, reply=incident.video,
                   reason=('prior_fall_observed' if incident.fall_seen and incident.video
                           and incident.video.assessment is VideoAssessment.NORMAL_ACTIVITY
                           else None),
                   approach_target=incident.approach_target,
                   revisit_target=incident.revisit_target)
        if incident.approach_target is not None:
            running = self._running_missions
            self._emit('approach_started', incident, question_id=incident.question_id,
                       reason=('patrol_stopped' if 'patrol' in running
                               else 'follow_stopped' if 'follow_person' in running else None))
        return incident.question_id

    # ------------------------------------------------------------ approach

    def set_running_missions(self, capability_ids):
        """Manager missions now; recorded when a check interrupts one."""
        self._running_missions = tuple(capability_ids)

    def _approach_target(self, incident):
        """A map point when a person is not certain there; None asks right away."""
        if incident.subject_key is not None:
            latest = self._subject_evidence.latest(incident.subject_key)
            if latest is not None and latest[2].strong is True and latest[2].association_usable:
                return None  # A clear Pose person: no need to look closer.
        return self._spot(incident)

    def _spot(self, incident):
        """The suspicion's map point, or None (approach off, no map point measured)."""
        if not self.approach_enabled or self._place is None:
            return None
        if incident.subject_key is None:
            return incident.scene_points[-1] if incident.scene_points else None
        latest = self._subject_evidence.latest(incident.subject_key)
        if latest is None or latest[2].box is None:
            return None
        try:
            return self._place.locate_near(latest[0], latest[2].box)
        except Exception:
            return None  # Auxiliary: never stops the question.

    def _remember_first_scene(self, incident):
        """Keep up to two frames of the scene that raised the question, with the box."""
        iid = incident.incident_id
        if iid in self._first_scenes:
            return
        try:
            window = self.buffer.window(end=self._now(), duration_s=2.0, max_images=2,
                                        max_age_s=self.policy.max_frame_age_s)
        except ValueError:
            return  # Nothing fresh: a later comparison is simply unclear.
        if incident.subject_key is None:
            box = incident.scene_boxes[-1] if incident.scene_boxes else None
        else:
            latest = self._subject_evidence.latest(incident.subject_key)
            box = latest[2].box if latest is not None else None
        self._first_scenes[iid] = (window, box)
        while len(self._first_scenes) > FIRST_SCENES_MAX:
            self._first_scenes.popitem(last=False)

    def approach_result(self, *, incident_id, question_id, evidence_revision, outcome):
        """The drive ended; on arrival look for a person before anyone is asked."""
        if outcome not in APPROACH_OUTCOMES:
            raise ValueError('invalid approach outcome')
        incident = self._incidents.get(incident_id)
        if (incident is None or incident.state is IncidentState.RESOLVED
                or incident.question_id != question_id):
            return False
        target, incident.approach_target = incident.approach_target, None
        self._emit('approach_completed', incident, question_id=question_id, reason=outcome)
        if outcome == 'arrived' and target is not None:
            now = self._now()
            self._person_checks[incident_id] = PersonCheck(
                question_id, target, now, now + PERSON_POSE_WAIT_S)
            self._advance_person_checks()
        return True

    def return_result(self, *, incident_id, question_id, evidence_revision, outcome):
        self._spot_checks.pop(incident_id, None)
        incident = self._incidents.get(incident_id)
        if incident is None:
            return False
        self._emit('approach_returned', incident, question_id=question_id,
                   reason='returned' if outcome == 'returned' else 'return_failed')
        return True

    # ------------------------------------------------------------ waiting and revisit

    def _unasked(self, incident_id, question_id):
        """The open incident still waiting for this question, or None."""
        identifier(incident_id)
        identifier(question_id)
        incident = self._incidents.get(incident_id)
        if (incident is None or incident.state is IncidentState.RESOLVED
                or incident.question_id != question_id or incident.answer is not None):
            return None
        return incident

    def check_waiting(self, *, incident_id, question_id, evidence_revision, reason):
        """The fall rules could not ask yet: 직접 움직이기 or another job (recorded only)."""
        if reason not in WAITING_REASONS:
            raise ValueError('invalid waiting reason')
        incident = self._unasked(incident_id, question_id)
        if incident is None:
            return False
        self._emit('check_waiting', incident, question_id=question_id, reason=reason)
        return True

    def check_not_confirmed(self, *, incident_id, question_id, evidence_revision, reason):
        """A minute of waiting: the guardian is alerted (urgent, once); the case stays open."""
        if reason not in NOT_CONFIRMED_REASONS:
            raise ValueError('invalid not-confirmed reason')
        incident = self._unasked(incident_id, question_id)
        if incident is None:
            return False
        if incident.state is not IncidentState.HELP_REQUIRED:
            incident.state = IncidentState.RECHECK_REQUIRED
        self._emit('check_not_confirmed', incident, question_id=question_id, reason=reason)
        self._notify_once(incident, NotificationLevel.URGENT, 'robot_could_not_confirm')
        return True

    def revisit_started(self, *, incident_id, question_id, evidence_revision, reason):
        """The robot drives back to the spot before asking; that spot is this incident's."""
        if reason not in WAITING_REASONS:
            raise ValueError('invalid revisit reason')
        incident = self._unasked(incident_id, question_id)
        if incident is None:
            return False
        self._emit('revisit_started', incident, question_id=question_id, reason=reason)
        if incident.revisit_target is not None:
            self._spot_checks[incident_id] = (incident.revisit_target, self._now())
        return True

    def revisit_result(self, *, incident_id, question_id, evidence_revision, outcome):
        """Back at the spot: look again as on the first approach, without closing."""
        if outcome not in APPROACH_OUTCOMES:
            raise ValueError('invalid revisit outcome')
        incident = self._unasked(incident_id, question_id)
        if incident is None:
            return False
        self._emit('approach_completed', incident, question_id=question_id, reason=outcome)
        if outcome == 'arrived' and incident.revisit_target is not None:
            now = self._now()
            self._person_checks[incident_id] = PersonCheck(
                question_id, incident.revisit_target, now, now + PERSON_POSE_WAIT_S, revisit=True)
            self._advance_person_checks()
        return True

    def _compare_later(self, iid, question_id, off_floor):
        """Compare the person seen now with the first scene; the robot never waits for it.

        After an off-floor look the guardian's notification follows the comparison.
        """
        first = self._first_scenes.get(iid)
        after = None
        if first is not None and self._cloud_block(self._now()) is None:
            try:
                after = self.buffer.window(end=self._now(), duration_s=2.0, max_images=2,
                                           max_age_s=self.policy.max_frame_age_s)
            except ValueError:
                after = None
        if after is None or not callable(getattr(self._provider, 'compare_person', None)):
            self._finish_comparison(iid, question_id, off_floor, 'unclear', None)
            return
        self._comparisons.append((iid, question_id, off_floor, first, after, self._now()))

    async def _cloud_same_person(self, iid, question_id, off_floor, first, after, _queued):
        incident = self._incidents.get(iid)
        if incident is None or incident.state is IncidentState.RESOLVED:
            return True
        now = self._now()
        if self._cloud_block(now) is not None:
            self._finish_comparison(iid, question_id, off_floor, 'unclear', None)
            return True
        request = SamePersonCheckRequest(str(uuid4()), first[0], first[1], after)
        self._calls.append(now)
        epoch, reply = self._epoch, None
        try:
            self._task = asyncio.create_task(self._provider.compare_person(request))
            self._task.add_done_callback(self._consume_exception)
            done, _ = await asyncio.wait({self._task}, timeout=self.policy.cloud_timeout_s)
            if not done:
                self._task.cancel()
            elif self._epoch == epoch and not self._task.cancelled():
                reply = self._task.result()
        except asyncio.CancelledError:
            if self._task is not None:
                self._task.cancel()
            raise
        except Exception:
            reply = None  # Blocked, timed out or malformed: unclear.
        verdict, analysis = 'unclear', None
        if isinstance(reply, SamePersonCheckReply):
            verdict = reply.verdict
            try:
                analysis = CloudAnalysisExplanation(
                    request.request_id, 'same_person_check', VideoAssessment.UNOBSERVABLE,
                    reply.explanation)
            except (ValueError, TypeError, UnicodeError):
                analysis = None
        self._finish_comparison(iid, question_id, off_floor, verdict, analysis)
        return True

    def _finish_comparison(self, iid, question_id, off_floor, verdict, analysis):
        incident = self._incidents.get(iid)
        if incident is None or incident.state is IncidentState.RESOLVED:
            return
        self._emit('same_person_compared', incident, question_id=question_id, reason=verdict,
                   analysis=analysis)
        if not off_floor:
            return  # Asked as before: the answer decides the notification.
        if verdict == 'same':
            self._notify_once(incident, NotificationLevel.INFO, 'revisit_same_person_off_floor')
        else:
            self._notify_once(incident, NotificationLevel.CHECK,
                              'revisit_person_unclear_off_floor')

    def _expire_comparisons(self):
        now = self._now()
        while self._comparisons and now - self._comparisons[0][5] > COMPARE_WAIT_S:
            iid, question_id, off_floor, *_ = self._comparisons.popleft()
            self._finish_comparison(iid, question_id, off_floor, 'unclear', None)

    def close_by_guardian(self, incident_id):
        """A member pressed 처리 완료 in the app: the robot stops checking this incident."""
        identifier(incident_id)
        incident = self._incidents.get(incident_id)
        if incident is None or incident.state is IncidentState.RESOLVED:
            return False  # Unknown here (another boot) or already closed: the list repeats.
        if self._active_incident == incident_id:
            # Its analysis is in flight; a result for a closed case is not recordable.
            self._guardian_closing.add(incident_id)
            return True
        self._guardian_closing.discard(incident_id)
        self._person_checks.pop(incident_id, None)
        self._spot_checks.pop(incident_id, None)
        self._first_scenes.pop(incident_id, None)
        self._comparisons = deque(c for c in self._comparisons if c[0] != incident_id)
        incident.pending = False
        incident.state = IncidentState.RESOLVED
        incident.close_reason = 'guardian_closed'
        self._emit('incident_resolved', incident, reason='guardian_closed')
        return True

    def _close_deferred(self):
        for incident_id in tuple(self._guardian_closing):
            self._guardian_closing.discard(incident_id)
            self.close_by_guardian(incident_id)

    def _spot_under_check(self, subject_key):
        """The incident checking where this Pose subject is, or None (farther than 1 m)."""
        now = self._now()
        for iid, (_, since) in tuple(self._spot_checks.items()):
            if iid not in self._incidents or now - since > SPOT_CHECK_MAX_S:
                del self._spot_checks[iid]
        if not self._spot_checks:
            return None
        latest = self._subject_evidence.latest(subject_key)
        if latest is None or latest[2].box is None:
            # Not measured yet while the robot faces the spot; it comes again with a box.
            return next(iter(self._spot_checks))
        stamp, _, pose = latest
        point = None
        if self._place is not None:
            try:
                point = self._place.locate_near(stamp, pose.box)
            except Exception:
                point = None
        for iid, (target, _) in self._spot_checks.items():
            if point is not None:
                if math.dist(point, target) <= SAME_PLACE_M:
                    return iid
            elif 0.25 <= (pose.box[0] + pose.box[2]) / 2 <= 0.75:
                return iid  # Same fallback as the close look: the robot faces the spot.
        return None

    def _same_person_incident(self, candidate):
        """The open person incident this track is the same person as, or None.

        Same moment: boxes overlap at SAME_PERSON_IOU. One after another: the
        track appears where an incident track vanished, within SAME_PERSON_GAP_S
        and without camera movement. Map points over SAME_PLACE_M apart are two
        people. A newly seen fall still opens its own incident.
        """
        key = candidate.subject_key
        if candidate.kind is CandidateKind.MOTION_SEEN:
            self._same_person.pop(key, None)
            return None
        members = {}
        for track, iid in tuple(self._same_person.items()):
            incident = self._incidents.get(iid)
            if incident is None or incident.state is IncidentState.RESOLVED:
                del self._same_person[track]
            else:
                members[track] = incident
        if key in members:
            return members[key]
        for incident in self._incidents.values():
            if incident.subject_key is not None and incident.state is not IncidentState.RESOLVED:
                members[incident.subject_key] = incident
        if not members:
            return None
        tracks, wanted = {}, members.keys() | {key}
        for stamp, poses in self._subject_evidence.frames_since(-math.inf):
            for pose in poses:
                if pose.box is not None and pose.subject_key in wanted:
                    tracks.setdefault(pose.subject_key, {})[stamp] = pose.box
        mine = tracks.pop(key, None)
        if not mine:
            return None
        first = min(mine)
        for track, theirs in tracks.items():
            together = mine.keys() & theirs.keys()
            if together:
                stamp = max(together)
                a, b = (stamp, theirs[stamp]), (stamp, mine[stamp])
                same = box_iou(a[1], b[1]) >= SAME_PERSON_IOU
            else:
                stamp = max(theirs)
                a, b = (stamp, theirs[stamp]), (first, mine[first])
                same = (stamp < first and first - stamp <= SAME_PERSON_GAP_S
                        and not self._camera_motion.moved_between(stamp, first)
                        and (box_iou(a[1], b[1]) >= SAME_PERSON_IOU
                             or _inside(a[1], b[1]) >= SAME_PERSON_INSIDE))
            if same and not self._apart_on_map(a, b):
                incident = members[track]
                self._same_person[key] = incident.incident_id
                if self._place_log is not None:
                    try:
                        self._place_log(f'fall place: Pose track {key} is the same person as '
                                        f'case {incident.incident_id[:8]}: no new case')
                    except Exception:
                        pass
                return incident
        return None

    def _apart_on_map(self, a, b):
        """Both (stamp, box) measured on the map, farther apart than one place."""
        if self._place is None:
            return False
        try:
            points = [self._place.locate_near(stamp, box) for stamp, box in (a, b)]
        except Exception:
            return False  # Auxiliary: never stops fall handling.
        return None not in points and math.dist(*points) > SAME_PLACE_M

    def _pose_sees_person(self, check):
        for stamp, poses in self._subject_evidence.frames_since(check.started):
            for pose in poses:
                if pose.strong is not True or not pose.association_usable or pose.box is None:
                    continue
                point = None
                if self._place is not None:
                    try:
                        point = self._place.locate_near(stamp, pose.box)
                    except Exception:
                        point = None
                if point is not None:
                    if math.dist(point, check.target) <= SAME_PLACE_M:
                        return True
                elif 0.25 <= (pose.box[0] + pose.box[2]) / 2 <= 0.75:
                    return True  # The robot faces the spot: a person in the middle.
        return False

    def _advance_person_checks(self):
        now = self._now()
        for iid, check in tuple(self._person_checks.items()):
            incident = self._incidents.get(iid)
            if (incident is None or incident.state is IncidentState.RESOLVED
                    or incident.question_id != check.question_id):
                del self._person_checks[iid]
            elif check.cloud:
                continue
            elif check.stage == 'view':
                if now >= check.deadline:
                    self._finish_posture_check(iid, 'body_unseen')  # Lost move report.
            elif check.stage == 'posture':
                if now >= check.deadline:
                    check.cloud = True  # run_once takes the next Cloud look.
            elif self._pose_sees_person(check):
                if self.full_view_enabled:
                    self._start_view(iid, check, now)
                else:
                    self._finish_person_check(iid, 'person')
            elif now >= check.pose_deadline:
                check.cloud = True  # run_once asks Cloud once.

    # ------------------------------------------------------------ whole body

    def _start_view(self, iid, check, now, analysis=None):
        """A person, seen only in part from 1 m: the coordinator moves to see all of it."""
        incident = self._incidents[iid]
        check.stage, check.cloud, check.asked = 'view', False, False
        check.deadline = now + VIEW_LIMIT_S + 30.0  # Beyond the coordinator's own limit.
        self._emit('person_check_completed', incident, question_id=check.question_id,
                   reason='partial_body', analysis=analysis)

    def _start_posture_check(self, iid, check, now):
        """Let the camera settle after the move, then the first Cloud look."""
        check.stage, check.cloud, check.asked, check.looks = 'posture', False, False, ()
        check.started, check.deadline = now, now + POSTURE_SETTLE_S

    def view_result(self, *, incident_id, question_id, evidence_revision, outcome):
        """The move to the whole-body spot ended; on arrival read the posture."""
        if outcome not in APPROACH_OUTCOMES:
            raise ValueError('invalid view outcome')
        incident = self._incidents.get(incident_id)
        check = self._person_checks.get(incident_id)
        if (incident is None or incident.state is IncidentState.RESOLVED
                or incident.question_id != question_id or check is None or check.stage != 'view'):
            return False
        self._emit('view_completed', incident, question_id=question_id, reason=outcome)
        if outcome == 'arrived':
            self._start_posture_check(incident_id, check, self._now())
        else:
            self._finish_posture_check(incident_id, 'body_unseen')
        return True

    async def _cloud_posture_check(self, iid, check):
        check.asked = True
        now = self._now()
        block = self._cloud_block(now)
        window = None
        if block is None:
            try:
                window = self.buffer.window(end=now, duration_s=2.0, max_images=3,
                                            max_age_s=self.policy.max_frame_age_s)
            except ValueError:
                block = 'fresh_rgb_unavailable'
        if block is not None:
            self._finish_posture_check(iid, 'posture_unconfirmed')  # Cannot agree: ask.
            return True
        request = PostureCheckRequest(str(uuid4()), window)
        self._calls.append(now)
        epoch, reply = self._epoch, None
        try:
            self._task = asyncio.create_task(self._provider.check_posture(request))
            self._task.add_done_callback(self._consume_exception)
            done, _ = await asyncio.wait({self._task}, timeout=self.policy.cloud_timeout_s)
            if not done:
                self._task.cancel()
            elif self._epoch == epoch and not self._task.cancelled():
                reply = self._task.result()
        except asyncio.CancelledError:
            if self._task is not None:
                self._task.cancel()
            raise
        except Exception:
            reply = None  # Unclear or failed: ask rather than close.
        verdict = reply.verdict if isinstance(reply, PostureCheckReply) else None
        analysis = None
        if verdict is not None:
            seen = (VideoAssessment.NORMAL_ACTIVITY if verdict in OFF_FLOOR_LOOKS
                    else VideoAssessment.SUSPECTED_FALL)
            try:
                analysis = CloudAnalysisExplanation(
                    request.request_id, 'posture_check', seen, reply.explanation)
            except (ValueError, TypeError, UnicodeError):
                analysis = None
        check.looks += (verdict or 'failed',)
        if verdict in OFF_FLOOR_LOOKS and len(check.looks) < 2:
            # One look is not enough to skip the question: look again on new frames.
            check.cloud, check.asked = False, False
            check.deadline = self._now() + POSTURE_GAP_S
            return True
        if verdict in OFF_FLOOR_LOOKS:
            reason = ('standing' if all(v == 'standing' for v in check.looks)
                      else 'sitting_off_floor')
        else:
            reason = 'on_floor' if verdict == 'floor' else 'posture_unconfirmed'
        self._finish_posture_check(iid, reason, analysis)
        return True

    def _finish_posture_check(self, iid, reason, analysis=None):
        check = self._person_checks.pop(iid, None)
        incident = self._incidents.get(iid)
        if check is None or incident is None or incident.state is IncidentState.RESOLVED:
            return
        if check.revisit:
            # Off the floor: the rules drive back without asking and the guardian is
            # told after the comparison. Anything else is asked as before.
            self._emit('posture_check_completed', incident, question_id=check.question_id,
                       reason=reason, analysis=analysis)
            self._compare_later(iid, check.question_id, reason in OFF_FLOOR_REASONS)
            return
        if reason in OFF_FLOOR_REASONS:
            if self._active_incident == iid:
                reason = 'posture_unconfirmed'  # Its analysis is in flight: ask instead.
            else:
                incident.pending = False
        if self._place_log is not None:
            try:
                self._place_log(f'fall posture: case {iid[:8]} Cloud looks '
                                f'{", ".join(check.looks) or "none"}: {reason}')
            except Exception:
                pass  # A log line never stops fall handling.
        self._emit('posture_check_completed', incident, question_id=check.question_id,
                   reason=reason, analysis=analysis)
        if reason in OFF_FLOOR_REASONS:
            self.resolve(iid, revision=incident.revision, reason='off_floor_verified')

    async def _cloud_person_check(self, iid, check):
        check.asked = True
        now = self._now()
        block = self._cloud_block(now)
        window = None
        if block is None:
            try:
                window = self.buffer.window(end=now, duration_s=2.0, max_images=3,
                                            max_age_s=self.policy.max_frame_age_s)
            except ValueError:
                block = 'fresh_rgb_unavailable'
        if block is not None:
            self._finish_person_check(iid, 'person')  # Cannot look: ask.
            return True
        request = PersonCheckRequest(str(uuid4()), window)
        self._calls.append(now)
        epoch, reply = self._epoch, None
        try:
            self._task = asyncio.create_task(self._provider.check_person(request))
            self._task.add_done_callback(self._consume_exception)
            done, _ = await asyncio.wait({self._task}, timeout=self.policy.cloud_timeout_s)
            if not done:
                self._task.cancel()
            elif self._epoch == epoch and not self._task.cancelled():
                reply = self._task.result()
        except asyncio.CancelledError:
            if self._task is not None:
                self._task.cancel()
            raise
        except Exception:
            reply = None  # Unclear or failed: ask rather than close.
        not_person = isinstance(reply, PersonCheckReply) and reply.verdict == 'not_person'
        analysis = None
        if isinstance(reply, PersonCheckReply):
            seen = (VideoAssessment.NORMAL_ACTIVITY if not_person
                    else VideoAssessment.SUSPECTED_FALL)
            try:
                analysis = CloudAnalysisExplanation(
                    request.request_id, 'person_check', seen, reply.explanation)
            except (ValueError, TypeError, UnicodeError):
                analysis = None
        if (self.full_view_enabled and isinstance(reply, PersonCheckReply)
                and reply.verdict == 'person' and iid in self._person_checks):
            # A person, but Pose did not see the whole body in time.
            self._start_view(iid, check, self._now(), analysis)
            return True
        self._finish_person_check(iid, 'not_a_person' if not_person else 'person', analysis)
        return True

    def _finish_person_check(self, iid, reason, analysis=None):
        check = self._person_checks.pop(iid, None)
        incident = self._incidents.get(iid)
        if check is None or incident is None or incident.state is IncidentState.RESOLVED:
            return
        if check.revisit:
            if reason == 'not_a_person':
                # Nobody there is no proof of anything: they may have crawled away or
                # fallen elsewhere. Kept open; the guardian checks (목업 28번).
                self._emit('person_check_completed', incident, question_id=check.question_id,
                           reason='nobody_there', analysis=analysis)
                self._notify_once(incident, NotificationLevel.CHECK, 'revisit_nobody_there')
                return
            self._emit('person_check_completed', incident, question_id=check.question_id,
                       reason=reason, analysis=analysis)
            self._compare_later(iid, check.question_id, False)  # Asked; compared meanwhile.
            return
        if reason == 'not_a_person':
            if self._active_incident == iid:
                reason = 'person'  # Its analysis is in flight: ask instead of closing.
            else:
                incident.pending = False
        self._emit('person_check_completed', incident, question_id=check.question_id,
                   reason=reason, analysis=analysis)
        if reason == 'not_a_person':
            self.resolve(iid, revision=incident.revision, reason='not_a_person')

    def _continue_confirmation(self, incident):
        """Reassess the latest evidence once an older question has finished."""
        if incident.state is IncidentState.HELP_REQUIRED:
            return
        incident.question_id = None
        if (incident.video is None or incident.video_revision != incident.revision
                or incident.pending or self._active_incident == incident.incident_id):
            return
        if (incident.video.assessment is not VideoAssessment.NORMAL_ACTIVITY
                or incident.fall_seen):
            self.ask_question(incident.incident_id)
        else:
            self._emit('analysis_completed', incident, reply=incident.video)

    def confirmation_result(self, *, incident_id, question_id, subject_key,
                            evidence_revision, situation_assessment, help_needed):
        """Apply the Manager's final result, preserving the user's own account.

        No transport failure is converted into a user's silence here. The
        conversation Agent alone applies its ten-second response deadline.
        """
        for value in (incident_id, question_id):
            identifier(value)
        # Asked instead of driving back: the check of the spot is over.
        self._spot_checks.pop(incident_id, None)
        if subject_key is not None:
            identifier(subject_key)
        if (type(evidence_revision) is not int or evidence_revision < 1
                or not isinstance(situation_assessment, str)
                or situation_assessment not in {'confirmed_incident', 'resolved', 'unknown'}
                or type(help_needed) is not bool):
            raise ValueError('invalid confirmation result')
        current = self._incidents.get(incident_id)
        question = self._questions.get(incident_id, current)
        incident = (question if question is not None and current is not None
                    and question.revision != current.revision else current)
        if (incident is None or incident.question_id != question_id
                or incident.subject_key != subject_key
                or incident.revision != evidence_revision
                or incident.answer is VoiceAnswer.FAILED
                or (incident is not current and current.state is IncidentState.RESOLVED)):
            return False
        if incident.situation_assessment is not None:
            return (incident.situation_assessment == situation_assessment
                    and incident.help_needed == help_needed)
        if incident.state is IncidentState.RESOLVED:
            return False
        incident.situation_assessment = situation_assessment
        incident.help_needed = help_needed
        incident.pending = False
        if incident is not current:
            # This answer belongs to the question's original evidence, not to
            # observations collected while the user was answering it.
            incident.answer = (VoiceAnswer.HELP if help_needed else
                               VoiceAnswer.UNCLEAR if incident.subject_key is None
                               else VoiceAnswer.OKAY)
            incident.state = (IncidentState.HELP_REQUIRED if help_needed
                              else IncidentState.RECHECK_REQUIRED)
            self._emit('confirmation_completed' if help_needed else 'voice_result',
                       incident, question_id=question_id,
                       reason=('scene_answer_unassociated' if incident.answer is VoiceAnswer.UNCLEAR
                               else situation_assessment))
            if help_needed:
                current.state = IncidentState.HELP_REQUIRED
                current.answer = VoiceAnswer.HELP
                current.help_needed = True
                incident.notification_level = current.notification_level
                self._notify(incident, NotificationLevel.URGENT, 'confirmation_help_required')
                current.notification_level = incident.notification_level
            self._emit('incident_updated', current, reason='confirmation_finished_newer_evidence')
            self._continue_confirmation(current)
            return True
        if incident.subject_key is None and not help_needed:
            # The speaker has not been associated with the person(s) seen by
            # Cloud. Keep the reply, but never treat it as their clearance.
            incident.answer = VoiceAnswer.UNCLEAR
            if incident.state is not IncidentState.HELP_REQUIRED:
                incident.state = IncidentState.RECHECK_REQUIRED
            self._questions[incident_id] = replace(incident)
            self._emit('voice_result', incident, question_id=question_id,
                       reason='scene_answer_unassociated')
            self._emit('decision_required', incident, reason='target_unidentified')
            return True
        incident.answer = VoiceAnswer.HELP if help_needed else VoiceAnswer.OKAY
        incident.state = IncidentState.HELP_REQUIRED if help_needed else IncidentState.RESOLVED
        if not help_needed:
            incident.close_reason = ('risk_cleared' if situation_assessment == 'resolved'
                                     else 'response_completed')
        self._questions[incident_id] = replace(incident)
        self._emit('confirmation_completed', incident, question_id=question_id,
                   reason=situation_assessment)
        if help_needed:
            self._notify(incident, NotificationLevel.URGENT, 'confirmation_help_required')
        else:
            # This is an explicit user-informed decision, not automatic video
            # clearance. A later video result may not overturn it.
            self._emit('incident_resolved', incident, reason=incident.close_reason)
            self._remember_settled_subject(incident)
        return True

    def confirmation_failed(self, *, incident_id, question_id, evidence_revision,
                            reason='question_error'):
        """Record an unavailable/failed conversation without inventing an answer.

        The robot could not check by itself (speech off, a failed question, or ten
        minutes of waiting): the guardian is alerted (urgent, once).
        """
        if reason not in CHECK_FAILURE_REASONS:
            raise ValueError('invalid confirmation failure reason')
        self._spot_checks.pop(incident_id, None)
        current = self._incidents.get(incident_id)
        question = self._questions.get(incident_id, current)
        incident = (question if question is not None and current is not None
                    and question.revision != current.revision else current)
        if (incident is None or incident.question_id != question_id
                or incident.revision != evidence_revision
                or incident.state is IncidentState.RESOLVED
                or current.state is IncidentState.RESOLVED
                or incident.situation_assessment is not None):
            return False
        if incident.answer is VoiceAnswer.FAILED:
            return True
        incident.answer = VoiceAnswer.FAILED
        incident.state = IncidentState.RECHECK_REQUIRED
        self._emit('agent_check_failed', incident, question_id=question_id, reason=reason)
        self._notify_once(incident, NotificationLevel.URGENT, 'robot_could_not_confirm')
        if incident is not current:
            self._raise_level(current, NotificationLevel.URGENT)
        self._questions[incident_id] = replace(incident)
        if incident is not current:
            self._emit('incident_updated', current, reason='confirmation_finished_newer_evidence')
            self._continue_confirmation(current)
        return True

    def pending_questions(self):
        """Retransmit unfinished handoffs so a late-starting Manager can receive them."""
        events = []
        for current in self._incidents.values():
            if current.merged_into_incident_ids:
                # Retransmit the terminal tombstone too: a lost merge event
                # must not leave an old scene question alive in the Manager.
                events.append(FallRuntimeEvent(
                    event_id=current.incident_id, kind='incident_merged',
                    incident_id=current.incident_id, question_id=current.question_id,
                    evidence_revision=current.revision, confirmation_scope='scene',
                    reason='findings_associated',
                    merged_into_incident_ids=current.merged_into_incident_ids))
                continue
            if current.state is IncidentState.RESOLVED:
                continue
            incident = self._questions.get(current.incident_id, current)
            if incident.answer is not None:
                incident = current
            if (incident.state is IncidentState.RESOLVED or incident.answer is not None
                    or incident.video is None or incident.video_revision != incident.revision):
                continue
            normal = incident.video.assessment is VideoAssessment.NORMAL_ACTIVITY
            if incident.question_id is None and (not normal or incident.pending):
                continue
            events.append(FallRuntimeEvent(
                event_id=incident.question_id or incident.incident_id,
                kind='question_requested' if incident.question_id else 'analysis_completed',
                incident_id=incident.incident_id, question_id=incident.question_id,
                subject_key=incident.subject_key, evidence_revision=incident.revision,
                confirmation_scope='scene' if incident.subject_key is None else 'subject',
                reply=incident.video,
                reason='prior_fall_observed' if normal and incident.fall_seen else None,
                approach_target=current.approach_target, revisit_target=current.revisit_target))
        return tuple(events)

    def agent_reply(self, reply: AgentCheckReply) -> bool:
        """Agent owns playback, waiting, speaker association and answer interpretation."""
        if not isinstance(reply, AgentCheckReply):
            raise ValueError('invalid Agent reply')
        current = self._incidents.get(reply.incident_id)
        question = self._questions.get(reply.incident_id, current)
        incident = (question if question is not None and current is not None
                    and question.revision != current.revision else current)
        if (incident is None or incident.question_id != reply.question_id
                or incident.subject_key != reply.subject_key
                or incident.revision != reply.evidence_revision
                or incident.state is IncidentState.RESOLVED
                or current.state is IncidentState.RESOLVED):
            return False
        answer = reply.answer
        # A help request can supersede an earlier answer to this question.
        if incident.answer is not None and answer is not VoiceAnswer.HELP:
            return False
        incident.answer = answer
        incident.answer_question_played = reply.question_played
        if incident is not current:
            incident.notification_level = current.notification_level
            incident.state = IncidentState.RECHECK_REQUIRED
        if answer is VoiceAnswer.HELP:
            incident.state = IncidentState.HELP_REQUIRED
            self._notify(incident, NotificationLevel.URGENT, 'help_requested')
        else:
            if answer is VoiceAnswer.NO_RESPONSE:
                self._notify(incident, NotificationLevel.CHECK, 'person_no_response')
            if answer is VoiceAnswer.FAILED:
                self._emit('agent_check_failed', incident, reason='agent_failed')
        self._emit('voice_result', incident, reason=answer.value)
        if incident.incident_id in self._questions:
            self._questions[incident.incident_id] = replace(incident)
        if incident is not current:
            current.notification_level = incident.notification_level
            if answer is VoiceAnswer.HELP:
                current.state = IncidentState.HELP_REQUIRED
                current.answer = VoiceAnswer.HELP
                current.help_needed = True
            self._emit('incident_updated', current, reason='confirmation_finished_newer_evidence')
            self._continue_confirmation(current)
            return True
        if answer is not VoiceAnswer.HELP:
            self._decision_needed(incident)
        return True

    _LEVEL_ORDER = {NotificationLevel.INFO: 1, NotificationLevel.CHECK: 2,
                    NotificationLevel.URGENT: 3}

    def _raise_level(self, incident, level):
        """The incident's highest notification level so far, whatever the reason."""
        previous = incident.notification_level
        if previous is None or self._LEVEL_ORDER[previous] < self._LEVEL_ORDER[level]:
            incident.notification_level = level

    def _notify(self, incident: FallIncident, level: NotificationLevel, reason: str) -> None:
        """The older reasons: only a higher level than any of them sent before."""
        previous = self._legacy_levels.get(incident.incident_id)
        if previous is not None and self._LEVEL_ORDER[previous] >= self._LEVEL_ORDER[level]:
            return
        self._legacy_levels[incident.incident_id] = level
        self._raise_level(incident, level)
        self._emit('notification_requested', incident, reason=reason, notification_level=level)

    def _notify_once(self, incident: FallIncident, level: NotificationLevel, reason: str) -> None:
        """Could not check, and what was seen on going back: each once, at its own level."""
        sent = self._sent_notices.setdefault(incident.incident_id, set())
        if reason in sent:
            return
        sent.add(reason)
        self._raise_level(incident, level)
        self._emit('notification_requested', incident, reason=reason, notification_level=level)

    def _decision_needed(self, incident: FallIncident) -> None:
        self._refresh_subject(incident)
        if (incident.state is IncidentState.RESOLVED
                or incident.answer is None or incident.pending
                or self._active_incident == incident.incident_id):
            return
        if incident.video is None and incident.last_failure is None:
            return
        if incident.state not in {IncidentState.HELP_REQUIRED, IncidentState.RESOLVED}:
            incident.state = IncidentState.RECHECK_REQUIRED
        if incident.fall_seen and incident.answer is VoiceAnswer.OKAY:
            self._notify(incident, NotificationLevel.INFO, 'fall_observed_person_okay')
        if self._enabled and self._camera and normal_path_ready(incident):
            if normal_closure_supported(
                    incident, now=self._now(),
                    max_observation_age_s=self.policy.max_person_observation_age_s):
                self.resolve(incident.incident_id, revision=incident.revision,
                             reason='normal_verified')
                return
            if len(incident.normal_checks) == 1 and self.request_recheck(incident.incident_id):
                self._emit('decision_required', incident, reason='new_video_recheck_required')
                return
        self._emit('decision_required', incident)

    def request_recheck(self, incident_id: str) -> bool:
        incident = self._incidents[incident_id]
        if incident.state is IncidentState.RESOLVED:
            return False
        if incident.subject_key is None:
            # No target exists for an incident-specific request. Periodic
            # scene scans continue; do not turn a bystander's normal result
            # into a recheck of an unidentified person.
            self._emit('recheck_unavailable', incident, reason='target_unidentified')
            return False
        if self._runtime_cloud_block is not None:
            self._emit('recheck_unavailable', incident, reason=self._runtime_cloud_block)
            return False
        if incident.attempts > 0 and incident.rechecks >= self.policy.max_rechecks:
            self._emit('recheck_unavailable', incident, reason='recheck_limit')
            return False
        incident.pending = True
        return True

    def mark_unresolved(self, incident_id: str, *, revision: int,
                        suspicion_persists: bool) -> None:
        """Fusion caller supplies persistence, not an inferred medical fact."""
        incident = self._incidents[incident_id]
        if type(suspicion_persists) is not bool or revision != incident.revision:
            raise ValueError('invalid or stale decision')
        if incident.state is IncidentState.RESOLVED:
            return
        if (incident.pending or self._active_incident == incident_id
                or incident.answer is None):
            raise ValueError('verification still pending')
        if incident.state is not IncidentState.HELP_REQUIRED:
            incident.state = IncidentState.RECHECK_REQUIRED
        if (incident.attempts > 0 and incident.rechecks >= self.policy.max_rechecks
                and suspicion_persists):
            self._notify(incident, NotificationLevel.CHECK, 'check_required_not_confirmed_fall')

    def resolve(self, incident_id: str, *, revision: int, reason: str) -> None:
        """Only a verified fusion/operator decision may call this boundary."""
        incident = self._incidents[incident_id]
        if reason not in {'normal_verified', 'risk_cleared', 'response_completed', 'not_a_person',
                          'off_floor_verified'}:
            raise ValueError('verified closure reason required')
        if (revision != incident.revision or incident.pending
                or self._active_incident == incident_id):
            raise ValueError('stale decision or unfinished analysis')
        if incident.state is IncidentState.RESOLVED:
            return
        if reason == 'normal_verified':
            if not self._enabled or not self._camera or not normal_closure_supported(
                    incident, now=self._now(),
                    max_observation_age_s=self.policy.max_person_observation_age_s):
                raise ValueError('normal closure not supported')
        incident.state = IncidentState.RESOLVED
        incident.close_reason = reason
        self._emit('incident_resolved', incident, reason=reason)
        self._remember_settled_subject(incident)

    def _cloud_block(self, now: float) -> Optional[str]:
        if not self._enabled or not self._camera:
            return 'monitoring_disabled'
        if not self._consent:
            return 'cloud_consent_missing'
        if not self._connected:
            return 'cloud_disconnected'
        while self._calls and self._calls[0] <= now - 60:
            self._calls.popleft()
        if len(self._calls) >= self.policy.max_calls_per_minute:
            return 'cloud_rate_limit'
        return None

    def _scene_incident_versions(self):
        # Include closures/new YOLO candidates while inference is in flight.
        return {
            i.incident_id: (i.subject_key, i.revision, i.state, i.last_observed_at, i.pending)
            for i in self._incidents.values()
        }

    @staticmethod
    def _new_candidate_covered(current, before, after, times):
        # Only a first, unanswered/unanalysed candidate already present in
        # this video may consume its Cloud result. Newer falls need new video.
        return (not before and len(after) == 1 and current is not None
                and current.state is IncidentState.VERIFYING
                and current.revision == 1 and current.answer is None
                and current.attempts == 0 and current.pending
                and times[0] - .1 <= current.last_observed_at <= times[-1])

    def _record_crosscheck(self, request, reply, snapshot, versions, *,
                           pose_snapshot=None, pose_generation=None):
        if reply.assessment not in (
            VideoAssessment.OBSERVED_FALL, VideoAssessment.SUSPECTED_FALL,
        ):
            return  # A scene-level normal result never clears a person's case.
        times = tuple(f.captured_at for f in request.window.frames)
        analysis = CloudAnalysisExplanation.from_reply(request, reply)
        if pose_snapshot is None:
            # Compatibility for internal callers with an exact-only snapshot.
            pose_snapshot = tuple(((t, observations),) if observations else ()
                                  for t, observations in zip(times, snapshot))
        if pose_generation is None:
            pose_generation = self._subject_evidence.generation
        measured = pose_snapshot
        if pose_generation == self._subject_evidence.generation:
            measured = supplement_samples(
                pose_snapshot, self._subject_evidence.association_samples(times))
        findings = reply.findings or (CloudPersonFinding(
            reply.assessment, CandidateKind.MOTION_SEEN
            if reply.assessment is VideoAssessment.OBSERVED_FALL else CandidateKind.UNKNOWN),)
        # Strongest evidence first if a provider accidentally repeats a person.
        order = sorted(enumerate(findings), key=lambda pair:
                       pair[1].assessment is not VideoAssessment.OBSERVED_FALL)
        linked = {}
        scene_closed_during_scan = any(
            v[0] is None and v[2] is not IncidentState.RESOLVED
            and self._incidents[iid].state is IncidentState.RESOLVED
            for iid, v in versions.items())
        for index, finding in order:
            match = associate_timed_finding(finding, measured)
            reason = 'invalid_locations' if reply.localization_failed else match.reason
            iid = None
            if reason == 'matched':
                latest = self._subject_evidence.latest(match.subject_key)
                if (latest is None or latest[1] != match.token
                        or self._now() - latest[0] > self.policy.max_person_observation_age_s):
                    reason = 'current_target_unavailable'
                elif match.subject_key in linked:
                    iid = linked[match.subject_key]
                else:
                    before = {k: v for k, v in versions.items() if v[0] == match.subject_key}
                    after = {k: v for k, v in self._scene_incident_versions().items()
                             if v[0] == match.subject_key}
                    current = next((i for i in self._incidents.values()
                                    if i.subject_key == match.subject_key
                                    and i.state is not IncidentState.RESOLVED), None)
                    consume_initial = self._new_candidate_covered(current, before, after, times)
                    if before != after and not consume_initial:
                        reason = 'incident_changed_during_scan'
                    else:
                        if current and current.subject_association_token != match.token:
                            reason = 'incident_target_continuity_unverified'
                        else:
                            iid = self._merge_cloud_person(
                                request, finding, match.subject_key, match.token,
                                consume_initial=consume_initial, analysis=analysis)
                            if iid is None:
                                reason = 'incident_capacity'
                            else:
                                linked[match.subject_key] = iid
                                if self._incidents[iid].state is IncidentState.RESOLVED:
                                    reason = 'settled_episode_continues'
            subject_key = match.subject_key if iid else None
            if iid is None and scene_closed_during_scan:
                reason = 'incident_changed_during_scan'
            wait_for = None
            if (iid is None and len(findings) == 1 and reason in {
                    'no_matching_track', 'current_target_unavailable', 'track_unusable',
                    'incident_target_continuity_unverified'}):
                wait_for = self._association_wait.candidate(
                    finding, times, self._incidents, versions, now=self._now(), policy=self.policy)
            if iid is None and wait_for is None and reason != 'incident_changed_during_scan':
                if self._on_furniture(times, finding):
                    reason = 'on_furniture'
                else:
                    iid = self._record_unidentified_scene(request, finding, analysis=analysis)
                    if iid is None:
                        reason = 'incident_capacity'
            proof = None
            if subject_key and (measured != pose_snapshot
                                or any(len(group) == 2 for group in measured)):
                proof = self._pose_link_proof(
                    iid, self._incidents[iid].revision, match.token,
                    times, finding, measured)
            discovery = CloudDiscovery(
                str(uuid4()), request.request_id, index, finding,
                tuple(f.captured_at for f in request.window.frames), reason,
                subject_key, iid, timed_association_evidence(finding, measured), proof)
            if wait_for is not None:
                deadline = self._association_wait.deadline(wait_for, self._now(), self.policy)
                discovery = replace(discovery, association_review=CloudAssociationReview(
                    wait_for.incident_id, wait_for.revision, deadline))
            if iid and subject_key is None:
                scene = self._incidents[iid]
                if len(scene.unresolved_discovery_ids) < 256:
                    scene.unresolved_discovery_ids += (discovery.discovery_id,)
                else:
                    scene.discovery_overflow = True
            # Every finding remains distinct, including discoveries sharing a
            # scene-level verification. An incident ID does not prove identity.
            self._emit('cloud_discovery', reason=reason, discovery=discovery)
            if wait_for is not None:
                self._association_wait.pending[discovery.discovery_id] = PendingAssociation(
                    discovery, self._epoch, self._now(), wait_for.incident_id,
                    wait_for.revision, deadline)
            self._prune_discoveries()
            self._discoveries[discovery.discovery_id] = DeferredDiscovery(
                discovery, self._epoch, self._now(),
                self._incidents[iid].revision if iid else 0,
                pose_snapshot=measured, pose_generation=pose_generation,
                incident_versions=versions, analysis=analysis)
            while len(self._discoveries) > 256:
                self._discoveries.popitem(last=False)

    def _cancel_pending_associations(self):
        waiting = tuple(self._association_wait.pending.values())
        self._association_wait.clear()
        if self._storage_failed:
            return  # _emit's storage-failure path must not recursively write.
        for pending in waiting:
            self._finish_pending_association(pending, 'cancelled', 'association_wait_control_changed')

    def _finish_pending_association(self, pending, status, reason, *, incident_id=None,
                                    subject_key=None, proof=None):
        d = replace(pending.discovery, reason=reason, incident_id=incident_id,
                    subject_key=subject_key, association_link=proof,
                    association_review=replace(pending.discovery.association_review, status=status))
        self._emit('cloud_discovery_linked' if subject_key else 'cloud_discovery',
                   discovery=d, reason=reason)
        self._association_wait.pending.pop(d.discovery_id, None)
        entry = self._discoveries.get(d.discovery_id)
        if entry is not None:
            entry.discovery = d
            entry.source_revision = self._incidents[incident_id].revision if incident_id else 0
        return d

    def _retry_pending_associations(self):
        """Only measured, same-token late Pose can turn a wait into a link.

        Old-position overlap is deliberately insufficient. The wait has no
        source scene/question/answer to copy, and never rewrites the candidate
        incident's help state or completed confirmation.
        """
        if not all((self._enabled, self._camera, self._consent, self._connected)):
            return
        if self._runtime_cloud_block is not None:
            return
        for pending in tuple(self._association_wait.pending.values()):
            d = pending.discovery
            entry = self._discoveries.get(d.discovery_id)
            target = self._incidents.get(pending.candidate_id)
            if (entry is None or pending.epoch != self._epoch
                    or self._now() >= pending.deadline
                    or self._now() - pending.received_at > 2
                    or entry.pose_generation != self._subject_evidence.generation
                    or target is None or target.state is IncidentState.RESOLVED
                    or target.revision != pending.candidate_revision
                    or self._active_incident == target.incident_id):
                continue
            measured = supplement_samples(entry.pose_snapshot,
                self._subject_evidence.association_samples(d.sample_times))
            match = associate_timed_finding(d.finding, measured)
            latest = self._subject_evidence.latest(match.subject_key)
            if (match.reason != 'matched' or match.subject_key != target.subject_key
                    or match.token != target.subject_association_token or latest is None
                    or latest[1] != match.token
                    or self._now() - latest[0] > self.policy.max_person_observation_age_s):
                continue
            proof = self._pose_link_proof(target.incident_id, target.revision, match.token,
                                         d.sample_times, d.finding, measured)
            self._finish_pending_association(pending, 'matched', 'matched_after_association_wait',
                incident_id=target.incident_id, subject_key=target.subject_key, proof=proof)
            self._record_clip(target, d.sample_times[0], d.sample_times[-1],
                              anchor_kind='cloud_window', found_down=True)
            self._record_cloud_boxes(target, d.sample_times, d.finding)

    def _release_pending_associations(self):
        """A wait is finite; unresolved identity falls back to scene verification.

        No extra Cloud call, no stored JPEG and no transfer of the old answer.
        Reuse the original capture times for clips/overlays, not the deadline.
        """
        for pending in tuple(self._association_wait.pending.values()):
            target = self._incidents.get(pending.candidate_id)
            changed = (target is None or target.state is IncidentState.RESOLVED
                       or target.revision != pending.candidate_revision)
            scene_exists = any(i.subject_key is None and i.state is not IncidentState.RESOLVED
                               for i in self._incidents.values())
            if not (changed or scene_exists or self._now() >= pending.deadline):
                continue
            d = pending.discovery
            if self._on_furniture(d.sample_times, d.finding):
                self._finish_pending_association(pending, 'cancelled', 'on_furniture')
                continue
            entry = self._discoveries.get(d.discovery_id)
            iid = self._record_unidentified_observation(
                d.sample_times, d.finding, analysis=entry.analysis if entry else None)
            if iid is not None:
                scene = self._incidents[iid]
                if len(scene.unresolved_discovery_ids) < 256:
                    scene.unresolved_discovery_ids += (d.discovery_id,)
                else:
                    scene.discovery_overflow = True
            reason = ('incident_capacity' if iid is None else
                      'association_wait_candidate_changed' if changed else
                      'association_wait_scene_verification' if scene_exists else
                      'association_wait_expired')
            self._finish_pending_association(pending, 'verification_required', reason, incident_id=iid)

    def maintain_associations(self):
        """Local tick independent of in-flight Cloud calls and the scan interval."""
        if not self._storage_failed and self._person_checks:
            self._advance_person_checks()
        if not self._storage_failed and self._comparisons:
            self._expire_comparisons()
        if (self._storage_failed or self._runtime_cloud_block is not None
                or not all((self._enabled, self._camera, self._consent, self._connected))):
            return
        self._retry_pending_associations()
        self._release_pending_associations()

    def _pose_link_proof(self, source_id, revision, token, times, finding, measured):
        return CloudPoseLink(
            source_id, revision, token, self._now(),
            tuple(times[r.frame_index] for r in finding.regions),
            tuple(tuple(t for t, _ in measured[r.frame_index]) for r in finding.regions))

    def _retry_pose_associations(self):
        """Recheck recent unmatched findings when measured Pose arrives.

        This path handles discoveries that already started scene verification;
        the bounded no-scene wait uses _retry_pending_associations instead.
        Never call Cloud again or transfer a scene's answer to a person. Source
        scene lifecycle remains explicit because it can contain other people.
        """
        self._prune_discoveries()
        for entry in tuple(self._discoveries.values()):
            d = entry.discovery
            if (d.subject_key is not None or d.association_link is not None
                    or d.reason not in {'no_matching_track', 'current_target_unavailable'}
                    or not entry.pose_snapshot or entry.incident_versions is None
                    or entry.pose_generation != self._subject_evidence.generation
                    or self._now() - entry.received_at > 2
                    or self._discovery_link_block(entry)):
                continue
            measured = supplement_samples(entry.pose_snapshot,
                self._subject_evidence.association_samples(d.sample_times))
            match = associate_timed_finding(d.finding, measured)
            if match.reason != 'matched':
                continue
            key, token = match.subject_key, match.token
            latest = self._subject_evidence.latest(key)
            if (latest is None or latest[1] != token
                    or self._now() - latest[0] > self.policy.max_person_observation_age_s):
                continue
            current = next((i for i in self._incidents.values() if i.subject_key == key
                            and i.state is not IncidentState.RESOLVED), None)
            # Do not create another question/person case solely because a late
            # Pose appeared. A later Pose candidate can supply the target case.
            if (current is None or current.subject_association_token != token
                    or self._active_incident == current.incident_id):
                continue
            before = {k: v for k, v in entry.incident_versions.items() if v[0] == key}
            after = {k: v for k, v in self._scene_incident_versions().items() if v[0] == key}
            if before != after:
                # A new candidate from the requested video is allowed. Revised,
                # answered or closed pre-existing cases must not be overwritten.
                if not self._new_candidate_covered(current, before, after, d.sample_times):
                    continue
            if any(other.discovery.discovery_id != d.discovery_id
                   and other.discovery.request_id == d.request_id
                   and other.discovery.subject_key == key
                   for other in self._discoveries.values()):
                continue
            proof = self._pose_link_proof(d.incident_id, entry.source_revision, token,
                                         d.sample_times, d.finding, measured)
            self._attach_discovery(entry, key, token, latest[0], current,
                                   proof=proof, reason='matched_after_pose_arrival')

    def _prune_discoveries(self):
        for did, entry in tuple(self._discoveries.items()):
            if self._now() - entry.received_at > 60:
                del self._discoveries[did]

    def _discovery_link_block(self, entry):
        if (not self._enabled or not self._camera or self._storage_failed
                or entry.epoch != self._epoch
                or self._runtime_cloud_block in {
                    'waiting_settings', 'disabled', 'camera_off', 'mapping',
                    'control_unavailable', 'runtime_error'}):
            return 'tracking_session_expired'
        source = self._incidents.get(entry.discovery.incident_id)
        if (source is None or source.state is IncidentState.RESOLVED
                or source.revision != entry.source_revision):
            return 'source_incident_changed'
        return None

    def begin_discovery_tracking(self, discovery_id):
        """Opt-in trusted local tracker boundary, NOT a Cloud/ROS command.

        Seed the tracker with the earliest region in this stored discovery and
        its exact RGB frame. Feed measured output with ingest_discovery_track.
        A broken track may not be restarted under this discovery's identity.
        """
        self._prune_discoveries()
        entry = self._discoveries.get(discovery_id)
        if entry is None:
            raise ValueError('unknown or expired discovery')
        discovery = entry.discovery
        if discovery.subject_key is not None:
            raise ValueError('discovery already associated')
        block = self._discovery_link_block(entry)
        if block:
            raise ValueError(block)
        if (not discovery.finding.regions or discovery.reason in {
                'invalid_locations', 'invalid_sample_index', 'incident_changed_during_scan'}
                or any(r.frame_index >= len(discovery.sample_times)
                       for r in discovery.finding.regions)):
            raise ValueError('discovery has no usable seed')
        if entry.track is None:
            entry.track = DiscoveryTrack(discovery)
            entry.session_id = str(uuid4())
        if entry.track.broken:
            raise ValueError('visual_track_broken')
        return entry.session_id

    def tracking_session_reason(self, session_id):
        """Read-only lifecycle gate for the local asynchronous tracker."""
        self._prune_discoveries()
        entry = next((e for e in self._discoveries.values()
                      if e.session_id is not None and e.session_id == session_id), None)
        if entry is None:
            return 'unknown_tracking_session'
        if entry.discovery.association_link is not None:
            return 'already_linked'
        return self._discovery_link_block(entry) or (
            'visual_track_broken' if entry.track.broken else None)

    def end_discovery_tracking(self, session_id):
        """A lost/stopped session cannot reacquire another person under its seed."""
        for entry in self._discoveries.values():
            if entry.session_id is not None and entry.session_id == session_id:
                if entry.discovery.association_link is None:
                    entry.track.broken = True
                return

    def pose_observation_ready(self, observed_at):
        return self._subject_evidence.observed_through(observed_at)

    def ingest_discovery_track(self, session_id, *, observed_at, box):
        """Associate a tracked discovery using exact, current measured Pose.

        Caller cannot choose the target ID. Lost masks, temporal gaps and
        ambiguous Pose do not establish a link. This never runs an inference.
        """
        timestamp(observed_at)
        self._prune_discoveries()
        entry = next((e for e in self._discoveries.values()
                      if e.session_id is not None and e.session_id == session_id), None)
        if entry is None:
            return DiscoveryLinkResult('unknown_tracking_session')
        if entry.discovery.association_link is not None:
            return DiscoveryLinkResult('already_linked', entry.discovery.incident_id)
        block = self._discovery_link_block(entry)
        if block:
            return DiscoveryLinkResult(block)
        if observed_at > self._now():
            raise ValueError('future visual observation')
        if not self.buffer.contains(observed_at):
            entry.track.broken = True
            return DiscoveryLinkResult('rgb_sample_unavailable')
        reason, match = entry.track.step(
            observed_at, box, self._subject_evidence.at(observed_at))
        if match is None:
            return DiscoveryLinkResult(reason)
        key, token = match
        latest = self._subject_evidence.latest(key)
        if (latest is None or latest[0] != observed_at or latest[1] != token
                or self._now() - observed_at > self.policy.max_person_observation_age_s):
            return DiscoveryLinkResult('current_target_unavailable')
        # Different findings from one Cloud reply are not declared the same
        # person merely because their trackers converge on one Pose box.
        if any(other.discovery.discovery_id != entry.discovery.discovery_id
               and other.discovery.request_id == entry.discovery.request_id
               and other.discovery.subject_key == key
               for other in self._discoveries.values()):
            return DiscoveryLinkResult('target_claimed_by_other_finding')
        current = next((i for i in self._incidents.values() if i.subject_key == key
                        and i.state is not IncidentState.RESOLVED), None)
        if current is not None:
            if current.subject_association_token != token:
                return DiscoveryLinkResult('incident_target_continuity_unverified')
            if self._active_incident == current.incident_id:
                return DiscoveryLinkResult('target_analysis_in_flight')
        elif any(i.subject_key == key and i.subject_association_token == token
                 and i.state is IncidentState.RESOLVED for i in self._incidents.values()):
            return DiscoveryLinkResult('target_incident_closed')
        elif not self._make_room():
            return DiscoveryLinkResult('incident_capacity')
        return self._attach_discovery(entry, key, token, observed_at, current)

    def _attach_discovery(self, entry, key, token, observed_at, current, *,
                          proof=None, reason='matched_after_tracking'):
        """Atomically link evidence and retire only a fully associated scene.

        Scene answers/alerts are never attributed to a person. A scene with
        another unidentified finding or an unassigned help request stays open.
        """
        discovery = entry.discovery
        finding = discovery.finding
        new = current is None
        evidence_time = discovery.sample_times[finding.regions[-1].frame_index]
        end = discovery.sample_times[-1]
        target = (FallIncident(
            str(uuid4()), key, finding.kind, self._now(), evidence_time,
            attempts=1, pending=False, subject_association_token=token,
            next_attempt_at=self._now() + self.policy.retry_interval_s)
            if new else replace(current))
        # Reuse the completed crosscheck for an unstarted first analysis when
        # it covers this candidate. Do not erase newer evidence or a requested
        # recheck, and never reset an already consumed attempt/recheck budget.
        if target.pending and target.attempts == 0 and end >= target.last_observed_at:
            target.pending = False
            target.attempts = 1
            target.next_attempt_at = self._now() + self.policy.retry_interval_s
        # Preserve confirmation already based on suspicious video of this
        # SAME target. An earlier answer with no video (or normal video) must
        # not clear the first newly attached suspicion.
        if (target.state is not IncidentState.HELP_REQUIRED
                and target.answer is not VoiceAnswer.HELP
                and ((target.answer is not None and target.video is None)
                     or (target.video is not None
                         and target.video.assessment is VideoAssessment.NORMAL_ACTIVITY))):
            target.revision += 1
            target.question_id = self._pending_question_id(target)
            target.answer = None
            target.answer_question_played = False
            target.situation_assessment = target.help_needed = None
            target.state = IncidentState.VERIFYING
        if 'cloud_crosscheck' not in target.candidate_sources:
            target.candidate_sources += ('cloud_crosscheck',)
        target.last_observed_at = max(target.last_observed_at, evidence_time)
        target.last_window_end = max(target.last_window_end, end)
        target.fall_seen |= finding.assessment is VideoAssessment.OBSERVED_FALL
        target.auto_normal_blocked = True
        target.normal_checks = ()
        target.subject_observation = None
        target.normal_evidence_after = max(target.normal_evidence_after, end, observed_at)
        if (target.video is None or
                target.video.assessment is not VideoAssessment.OBSERVED_FALL):
            target.video = CloudFallReply(finding.assessment, '해당 대상에 연결한 영상 근거')
            target.kind = finding.kind
        target.video_revision = target.revision
        if proof is None:
            proof = CloudDiscoveryLink(
                discovery.incident_id, entry.source_revision, token, entry.track.seed_time,
                observed_at, entry.track.samples, entry.track.pending[2])
        linked = replace(discovery, reason=reason, subject_key=key,
                         incident_id=target.incident_id, association_link=proof)
        events = []

        def stage(kind, **kwargs):
            events.append(FallRuntimeEvent(
                str(uuid4()), kind, incident_id=target.incident_id, subject_key=key,
                evidence_revision=target.revision, **kwargs))

        stage('incident_opened' if new else 'incident_updated', reason='cloud_crosscheck')
        stage('analysis_completed', reply=target.video, analysis=entry.analysis)
        if target.answer is None:
            if target.question_id is None:
                target.question_id = str(uuid4())
            # Keep an ongoing target conversation bound to its original
            # evidence even while the separate source scene is retired.
            question = self._questions.get(target.incident_id)
            if question is None or question.answer is not None:
                question = replace(target)
                stage('question_requested', question_id=target.question_id, reply=target.video)
        stage('cloud_discovery_linked', discovery=linked, reason=reason)
        source = self._incidents[discovery.incident_id]
        source = replace(source,
            unresolved_discovery_ids=tuple(did for did in source.unresolved_discovery_ids
                                          if did != discovery.discovery_id),
            associated_incident_ids=tuple(dict.fromkeys(
                (*source.associated_incident_ids, target.incident_id))))
        if (source.subject_key is None and not source.unresolved_discovery_ids
                and not source.discovery_overflow
                and len(source.associated_incident_ids) <= 128
                and discovery.discovery_id in self._incidents[source.incident_id].unresolved_discovery_ids
                and source.state is not IncidentState.HELP_REQUIRED
                and source.answer is not VoiceAnswer.HELP
                and source.notification_level is not NotificationLevel.URGENT):
            source.state = IncidentState.RESOLVED
            source.pending = False
            source.close_reason = 'findings_associated'
            source.merged_into_incident_ids = source.associated_incident_ids
            # Cancel the source before exposing a target question to Manager.
            events.insert(0, FallRuntimeEvent(
                str(uuid4()), 'incident_merged', incident_id=source.incident_id,
                evidence_revision=source.revision, question_id=source.question_id,
                confirmation_scope='scene', reason=source.close_reason,
                merged_into_incident_ids=source.merged_into_incident_ids))
        if self._journal is not None:
            try:
                self._journal.append_association(
                    device_id=self.device_id, boot_id=self.boot_id,
                    events=tuple(events), incident=target, source_incident=source)
            except Exception:
                self._storage_failed = True
                self.configure(enabled=False, camera_enabled=False,
                               cloud_consent=False, connected=False)
                raise FallJournalError('fall association persistence failed') from None
        # Atomic journal success precedes state/events visible to consumers.
        self._incidents[target.incident_id] = target
        self._incidents[source.incident_id] = source
        if target.answer is None:
            self._questions[target.incident_id] = question
        entry.discovery = linked
        self._events.extend(events)
        self._record_clip(target, discovery.sample_times[0], discovery.sample_times[-1],
                          anchor_kind='cloud_window',
                          found_down=finding.kind is CandidateKind.ALREADY_DOWN)
        self._record_cloud_boxes(target, discovery.sample_times, finding)
        return DiscoveryLinkResult(reason, target.incident_id)

    def _record_unidentified_scene(self, request, finding, *, analysis=None):
        """Start verification without Pose, reusing the completed Cloud result.

        A finding joins an open scene case only at the same place in the same
        camera view; otherwise it opens its own case and question (bounded by
        max_open_scene_cases). This is not person association: separate
        discoveries retain their own boxes, timestamps and reasons. No
        geometry/appearance guess joins a Pose case.
        """
        return self._record_unidentified_observation(
            tuple(f.captured_at for f in request.window.frames), finding, request=request,
            analysis=analysis)

    def _make_room(self):
        """Free a memory slot from finished history; the journal keeps every case.

        Only resolved cases or scene cases quiet for CASE_GAP_S with no open
        question, both last observed over CASE_GAP_S ago, are released.
        """
        if len(self._incidents) < self.policy.max_incidents:
            return True
        now = self._now()
        settled = {iid for _, iid in self._settled_subjects.values()}
        for iid, incident in self._incidents.items():
            last = max(incident.last_observed_at, incident.scene_seen_until or 0.0)
            finished = (incident.state is IncidentState.RESOLVED
                        or (incident.subject_key is None
                            and (incident.question_id is None or incident.answer is not None)))
            if (finished and now - last > CASE_GAP_S and not incident.pending
                    and iid != self._active_incident and iid not in settled
                    and not incident.merged_into_incident_ids):
                del self._incidents[iid]
                self._questions.pop(iid, None)
                self._association_wait.anchors.pop(iid, None)
                for key in [k for k in self._clip_offsets
                            if k == iid or (isinstance(k, tuple) and k[0] == iid)]:
                    del self._clip_offsets[key]
                return True
        return False

    def _map_points(self, times, finding):
        if self._place is None:
            return ()
        points = []
        locate3d = getattr(self._place, 'locate3d', None)
        for region in finding.regions:
            if region.frame_index < len(times):
                try:
                    if locate3d is not None:
                        spot = locate3d(times[region.frame_index], region.box)
                        self._log_place('Cloud box', spot)
                        point = None if spot is None else spot[:2]
                    else:
                        point = self._place.locate(times[region.frame_index], region.box)
                except Exception:
                    point = None  # Auxiliary: never stops fall handling.
                if point is not None:
                    points.append(point)
        return tuple(dict.fromkeys(points))

    def _on_furniture(self, times, finding):
        """Every box rests clearly above the floor and no fall was seen; else as before."""
        locate3d = getattr(self._place, 'locate3d', None)
        if locate3d is None or finding.assessment is VideoAssessment.OBSERVED_FALL:
            return False
        heights = []
        for region in finding.regions:
            if region.frame_index >= len(times):
                continue
            left, top, right, bottom = region.box
            band = (left, bottom - (bottom - top) * SUPPORT_BAND, right, bottom)
            try:
                spot = locate3d(times[region.frame_index], band)
            except Exception:
                spot = None
            if spot is None:
                return False  # Not measured: a fall cannot be ruled out.
            heights.append(spot[2])
        if not heights or min(heights) <= FURNITURE_HEIGHT_M:
            return False
        if self._place_log is not None:
            try:
                self._place_log(f'fall place: Cloud box rests {min(heights) * 100:.0f} cm above '
                                'the floor, on a bed or sofa: not a fall case')
            except Exception:
                pass
        return True

    def _log_place(self, label, spot):
        if self._place_log is None or spot is None or len(spot) != 3:
            return
        try:
            self._place_log(f'fall place: {label} at map ({spot[0]:.2f}, {spot[1]:.2f}), '
                            f'{spot[2] * 100:.0f} cm above the floor')
        except Exception:
            pass  # A log line never stops fall handling.

    def _log_pose_place(self, incident):
        locate3d_near = getattr(self._place, 'locate3d_near', None)
        latest = self._subject_evidence.latest(incident.subject_key)
        if locate3d_near is None or latest is None or latest[2].box is None:
            return
        try:
            spot = locate3d_near(latest[0], latest[2].box)
        except Exception:
            return
        self._log_place(f'Pose case {incident.incident_id[:8]}', spot)

    def _scene_case_for(self, times, boxes, points, request_id):
        """The open scene case this finding continues, or None for a new case."""
        open_scenes = [i for i in self._incidents.values() if i.subject_key is None
                       and i.state is not IncidentState.RESOLVED]
        if request_id is not None:
            for incident in open_scenes:
                if incident.scene_request_id == request_id:
                    return incident
        # After CASE_GAP_S without findings the situation ended. The case stays
        # open for its answer and history but takes no new findings.
        active = [i for i in open_scenes if i.scene_seen_until is not None
                  and times[0] - i.scene_seen_until <= CASE_GAP_S]
        for incident in reversed(active):
            if incident.scene_points and points:
                same = near_on_map(incident.scene_points, points)
            else:
                same = (not self._camera_motion.moved_between(incident.scene_seen_from, times[-1])
                        and same_place(incident.scene_boxes, boxes))
            if same:
                return incident
        if active and (len(active) >= self.max_open_scene_cases or not self._make_room()):
            # Full: keep the earlier single-case behaviour rather than dropping it.
            return active[-1]
        return None

    def _record_unidentified_observation(self, times, finding, *, request=None, analysis=None):
        boxes = tuple(dict.fromkeys(r.box for r in finding.regions))
        points = self._map_points(times, finding)
        current = self._scene_case_for(times, boxes, points,
                                       request.request_id if request else None)
        end = times[-1]
        new = current is None
        if new:
            if not self._make_room():
                return None
            current = FallIncident(
                str(uuid4()), None, finding.kind, self._now(), end,
                attempts=1, pending=False, candidate_sources=('cloud_crosscheck',),
                auto_normal_blocked=True, normal_evidence_after=end)
            self._incidents[current.incident_id] = current
        if boxes:
            current.scene_boxes = boxes
        if points:
            current.scene_points = points
        current.scene_seen_from = (times[0] if current.scene_seen_from is None
                                   else max(current.scene_seen_from, times[0]))
        current.scene_seen_until = (end if current.scene_seen_until is None
                                    else max(current.scene_seen_until, end))
        if request is not None:
            current.scene_request_id = request.request_id
        # A changed Cloud label alone is not a new episode. Keep the room's
        # outstanding/completed/failed question, retaining the stronger finding
        # for decisions without restarting the same confirmation conversation.
        escalation = (current.video is not None
                      and current.video.assessment is not VideoAssessment.OBSERVED_FALL
                      and finding.assessment is VideoAssessment.OBSERVED_FALL)
        if escalation:
            self._emit('incident_updated', current, reason='target_unidentified')
        current.last_observed_at = max(current.last_observed_at, end)
        current.last_window_end = max(current.last_window_end, end)
        current.fall_seen |= finding.assessment is VideoAssessment.OBSERVED_FALL
        if current.video is None or finding.assessment is VideoAssessment.OBSERVED_FALL:
            current.video = CloudFallReply(finding.assessment, '대상 미확인 영상의 의심 상황')
            current.kind = finding.kind
        current.video_revision = current.revision
        if new:
            self._emit('incident_opened', current, reason='target_unidentified')
        self._record_clip(current, times[0], times[-1], anchor_kind='cloud_window',
                          found_down=finding.kind is CandidateKind.ALREADY_DOWN)
        self._record_cloud_boxes(current, times, finding)
        self._emit('analysis_completed', current, request=request, reply=current.video,
                   reason='target_unidentified', analysis=analysis)
        if current.question_id is None:
            self.ask_question(current.incident_id)
        if current.answer is not None:
            self._emit('decision_required', current, reason='target_unidentified')
        return current.incident_id

    def _merge_cloud_person(self, request, finding, subject_key, token, *, consume_initial=False,
                          analysis=None):
        current = next((i for i in self._incidents.values() if i.subject_key == subject_key
                        and i.state is not IncidentState.RESOLVED), None)
        if current is None:
            settled = self._settled_subject(subject_key, token, finding)
            if settled is not None:
                # Preserve the final answer/state. The caller records this new
                # finding in cloud_discoveries, including why no question was
                # made. Do not emit a nonterminal event for a closed web case.
                self._record_window_clip(settled, request, finding)
                return settled.incident_id
        observed_at = request.window.frames[finding.regions[-1].frame_index].captured_at
        end = request.window.frames[-1].captured_at
        new = current is None
        if new:
            if not self._make_room():
                return None
            # The verified result may take 20 seconds: retain its real capture
            # time, not a fabricated fresh timestamp passed through candidate().
            current = FallIncident(
                str(uuid4()), subject_key, finding.kind, self._now(), observed_at,
                attempts=1, pending=False,
                next_attempt_at=self._now() + self.policy.retry_interval_s,
                candidate_sources=('cloud_crosscheck',), subject_association_token=token)
            self._incidents[current.incident_id] = current
        else:
            if 'cloud_crosscheck' not in current.candidate_sources:
                current.candidate_sources += ('cloud_crosscheck',)
            if consume_initial:
                current.pending = False
                current.attempts = 1
                current.next_attempt_at = self._now() + self.policy.retry_interval_s
            previous = current.video.assessment if current.video else None
            escalation = previous not in (VideoAssessment.OBSERVED_FALL,
                                          VideoAssessment.SUSPECTED_FALL) or (
                previous is VideoAssessment.SUSPECTED_FALL
                and finding.assessment is VideoAssessment.OBSERVED_FALL)
            if (escalation and not consume_initial
                    and current.answer is not VoiceAnswer.HELP
                    and current.question_id is None):
                current.revision += 1
                current.question_id = self._pending_question_id(current)
                current.answer = None
                current.answer_question_played = False
                current.situation_assessment = None
                current.help_needed = None
                self._emit('incident_updated', current, reason='cloud_crosscheck')
            current.last_observed_at = max(current.last_observed_at, observed_at)
        current.fall_seen |= finding.assessment is VideoAssessment.OBSERVED_FALL
        current.auto_normal_blocked = True
        current.normal_checks = ()
        current.subject_observation = None
        current.normal_evidence_after = max(current.normal_evidence_after, end)
        # A new suspicion must never downgrade an earlier observed fall.
        if (current.video is None or current.video.assessment is not VideoAssessment.OBSERVED_FALL
                or finding.assessment is VideoAssessment.OBSERVED_FALL):
            current.video = CloudFallReply(finding.assessment, '주기적 영상에서 해당 대상 확인')
            current.kind = finding.kind
        current.video_revision = current.revision
        current.last_window_end = max(current.last_window_end, end)
        current.last_failure = None
        if new:
            self._emit('incident_opened', current, reason='cloud_crosscheck')
        self._record_window_clip(current, request, finding)
        self._emit('analysis_completed', current, request=request, reply=current.video,
                   analysis=analysis)
        if current.question_id is None:
            self.ask_question(current.incident_id)
        self._decision_needed(current)
        return current.incident_id

    async def run_once(self) -> bool:
        """One attempt/scan; queued normal rechecks obey the existing budget."""
        if (self._runtime_cloud_block is not None or self._running
                or (self._task is not None and not self._task.done())):
            return False
        self._running = True
        try:
            return await self._run_once()
        finally:
            self._running = False
            self._active_incident = None
            if self._guardian_closing and not self._storage_failed:
                self._close_deferred()

    async def _run_once(self) -> bool:
        now = self._now()
        if not self._enabled or not self._camera:
            return False
        looking = next(((iid, c) for iid, c in self._person_checks.items()
                        if c.cloud and not c.asked), None)
        if looking is not None:
            if looking[1].stage == 'posture':
                return await self._cloud_posture_check(*looking)
            return await self._cloud_person_check(*looking)
        if self._comparisons:
            return await self._cloud_same_person(*self._comparisons.popleft())
        # Local evidence/verification policy does not depend on the next scan
        # or on receiving another successful Cloud response.
        self.maintain_associations()
        # First attempts before rechecks, both before periodic background scans.
        ready = [i for i in self._incidents.values() if i.pending
                 and i.state is not IncidentState.RESOLVED
                 and now >= i.next_attempt_at]
        ready.sort(key=lambda i: (i.attempts > 0, i.next_attempt_at, i.opened_at))
        incident = ready[0] if ready else None
        if incident is None and now < self._scan_anchor + self.periodic_interval_s():
            return False
        if incident is not None:
            if incident.attempts > 0 and incident.rechecks >= self.policy.max_rechecks:
                incident.pending = False
                self._emit('recheck_unavailable', incident, reason='recheck_limit')
                return True
        block = self._cloud_block(now)
        window = None
        if block is None:
            try:
                window = self.buffer.window(
                    end=now, duration_s=self.policy.clip_window_s,
                    max_images=self.policy.max_images,
                    max_age_s=self.policy.max_frame_age_s)
            except ValueError:
                block = 'fresh_rgb_unavailable'
        last_end = incident.last_window_end if incident else self._last_scan_end
        if window and window.frames[-1].captured_at <= last_end:
            # Wait for new RGB without consuming the retry budget.
            return False
        if incident is None:
            self._scan_anchor = now
        else:
            incident.rechecks += int(incident.attempts > 0)
            incident.attempts += 1
            incident.pending = False
            incident.next_attempt_at = now + self.policy.retry_interval_s
            if incident.state is not IncidentState.HELP_REQUIRED:
                incident.state = IncidentState.VERIFYING
        if block:
            self._failure(incident, block)
            return True
        sensors = incident.sensors if incident else None
        if sensors is not None and now - sensors.observed_at > self.policy.max_frame_age_s:
            sensors = None
        request = CloudFallRequest(
            request_id=str(uuid4()), purpose='incident' if incident else 'crosscheck',
            device_id=self.device_id, boot_id=self.boot_id,
            incident_id=incident.incident_id if incident else None,
            subject_key=incident.subject_key if incident else None,
            evidence_revision=incident.revision if incident else 0,
            window=window, sensors=sensors)
        if incident:
            request = replace(request, target=self._subject_evidence.target(
                incident.subject_key, window))
        scene_snapshot = self._subject_evidence.snapshot(window) if incident is None else ()
        pose_snapshot = (self._subject_evidence.association_samples(
            tuple(f.captured_at for f in window.frames)) if incident is None else ())
        pose_generation = self._subject_evidence.generation
        scene_versions = self._scene_incident_versions() if incident is None else {}
        if incident:
            incident.last_window_end = window.frames[-1].captured_at
        else:
            self._last_scan_end = window.frames[-1].captured_at
        epoch = self._epoch
        self._active_incident = request.incident_id
        self._calls.append(now)
        self._task = None
        self._analysis_status = CloudAnalysisStatus(
            'waiting_response', request.request_id, request.purpose)
        try:
            self._task = asyncio.create_task(self._provider.analyze(request))
            self._task.add_done_callback(self._consume_exception)
            done, _ = await asyncio.wait({self._task}, timeout=self.policy.cloud_timeout_s)
            if self._epoch != epoch or self._task.cancelled():
                self._failure(incident, 'cloud_permission_changed', request=request)
                return True
            if not done:
                self._task.cancel()
                self._failure(incident, 'cloud_timeout', request=request)
                return True
            reply = self._task.result()
            if not isinstance(reply, CloudFallReply):
                raise ValueError('invalid reply contract')
        except asyncio.CancelledError:
            if self._task is not None:
                self._task.cancel()
            self._failure(incident, 'worker_cancelled', request=request)
            raise
        except CloudFallProviderError as error:
            self._failure(incident, error.code, request=request)
            return True
        except Exception:
            self._failure(incident, 'cloud_failed_or_invalid_response', request=request)
            return True
        finally:
            self._active_incident = None
        self._analysis_status = CloudAnalysisStatus(
            'completed', request.request_id, request.purpose)
        if incident:
            if (incident.state is IncidentState.RESOLVED
                    or incident.situation_assessment is not None):
                self._emit('stale_analysis_result', incident,
                           reason='confirmation_already_completed', request=request, reply=reply)
                return True
            # A late result cannot clear new evidence, but an observed fall
            # in this incident's earlier video must not disappear from history.
            incident.fall_seen |= reply.assessment is VideoAssessment.OBSERVED_FALL
            if reply.assessment is not VideoAssessment.NORMAL_ACTIVITY:
                incident.auto_normal_blocked = True
            # Keep old evidence in events, but never clear a newer change with it.
            if request.evidence_revision != incident.revision:
                self._emit('stale_analysis_result', incident, reason=reply.assessment.value,
                           request=request, reply=reply)
                return True
            incident.video = reply
            incident.video_revision = request.evidence_revision
            incident.last_failure = None
            if reply.assessment is not VideoAssessment.NORMAL_ACTIVITY:
                # A Cloud-suspected/unobservable case is not the approved
                # YOLO-only + initial-normal automatic closure branch.
                incident.normal_checks = ()
                incident.subject_observation = None
            elif request.window.frames[-1].captured_at >= incident.normal_evidence_after:
                check = NormalVideoCheck(request.request_id, request.evidence_revision,
                                         request.window.frames[-1].captured_at,
                                         (request.target.association_token
                                          if request.target else None))
                checks = incident.normal_checks
                if not checks or check.window_end > checks[-1].window_end:
                    incident.normal_checks = (checks + (check,))[-2:]
            self._emit('analysis_completed', incident, request=request, reply=reply,
                       analysis=CloudAnalysisExplanation.from_reply(request, reply))
            if ((reply.assessment is not VideoAssessment.NORMAL_ACTIVITY or incident.fall_seen)
                    and incident.question_id is None):
                self.ask_question(incident.incident_id)
            self._decision_needed(incident)
        else:
            self._record_crosscheck(request, reply, scene_snapshot, scene_versions,
                                   pose_snapshot=pose_snapshot, pose_generation=pose_generation)
            self._emit('crosscheck_completed', request=request, reply=reply)
        return True

    def _failure(self, incident: Optional[FallIncident], reason: str, *, request=None) -> None:
        if request is not None:
            state = 'failed'
            error = reason
            if reason in {'cloud_permission_changed', 'worker_cancelled'}:
                state = ('cancel_requested' if self._task is not None
                         and not self._task.done() else 'canceled')
                error = ''
            self._analysis_status = CloudAnalysisStatus(
                state, request.request_id, request.purpose, error)
        if incident:
            if (request is not None
                    and (request.evidence_revision != incident.revision
                         or incident.state is IncidentState.RESOLVED
                         or incident.situation_assessment is not None)):
                self._emit('stale_analysis_result', incident, reason=reason, request=request)
                return
            incident.last_failure = reason
            # Earlier normal must not stand in for a failed new attempt.
            incident.video = None
            incident.video_revision = None
            incident.normal_checks = ()
            incident.subject_observation = None
            self._active_incident = None
            self._emit('analysis_unavailable', incident, reason=reason)
            self._retry_or_ask(incident, reason)
            self._decision_needed(incident)
        else:
            self._emit('crosscheck_skipped', reason=reason)

    def _retry_or_ask(self, incident, reason):
        """A Pose case nobody was asked about: send it again, then ask anyway.

        The retries use the existing recheck budget and interval. When every
        attempt failed, the video could not be judged; the person is asked on
        the Pose evidence rather than left without a question.
        """
        if (reason in GUARD_FAILURES or incident.subject_key is None
                or incident.question_id is not None or incident.answer is not None
                or incident.state in (IncidentState.RESOLVED, IncidentState.HELP_REQUIRED)):
            return
        if incident.rechecks < self.policy.max_rechecks:
            incident.pending = True  # next_attempt_at already spaces the retry.
            return
        incident.video = CloudFallReply(VideoAssessment.UNOBSERVABLE,
                                        '영상 분석 실패: 자세 감지 근거로 확인')
        incident.video_revision = incident.revision
        incident.auto_normal_blocked = True
        self.ask_question(incident.incident_id)

    @staticmethod
    def _consume_exception(task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()

    async def close(self) -> None:
        """Request cancellation; a broken adapter may still require termination."""
        self.configure(enabled=False, camera_enabled=False,
                       cloud_consent=False, connected=False)
        if self._task is not None:
            self._task.cancel()
