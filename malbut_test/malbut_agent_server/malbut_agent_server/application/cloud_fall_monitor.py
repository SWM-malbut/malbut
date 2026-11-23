"""Cloud-only fall workflow core, driven by an owning asyncio event loop.

Adapters ingest RGB/candidates and deliver events to speech/notification sinks.
This module never infers a spoken answer, drives the robot, sends a guardian
message, or imports the evaluation runner. Only the approved narrow normal
closure rule is automatic; other closure decisions remain explicit.
"""

import asyncio
from collections import OrderedDict, deque
from dataclasses import replace
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple
from uuid import uuid4

from malbut_agent_server.application.cloud_fall import (
    APPROACH_OUTCOMES,
    CHECK_FAILURE_REASONS,
    CLEARED_PLACE_MAX_S,
    CLEARED_REASONS,
    COMPARE_WAIT_S,
    FIRST_SCENES_MAX,
    FLOOR_ACTIVE_LOOK,
    FLOOR_ACTIVE_REASON,
    FURNITURE_HEIGHT_M,
    GUARD_FAILURES,
    LEVEL_ORDER,
    NOT_CONFIRMED_REASONS,
    OFF_FLOOR_LOOKS,
    OFF_FLOOR_REASONS,
    PERSON_POSE_WAIT_S,
    POSTURE_GAP_S,
    POSTURE_SETTLE_S,
    RUNTIME_CLOUD_BLOCKS,
    SAME_PERSON_GAP_S,
    SAME_PERSON_INSIDE,
    SAME_PERSON_IOU,
    SAME_PLACE_CASE_S,
    SPOT_CHECK_MAX_S,
    STOP_REASONS,
    SUPPORT_BAND,
    VIEW_LIMIT_S,
    WAITING_REASONS,
    CloudAnalysisStatus,
    PersonCheck,
    _inside,
    approach,
    associations,
    confirmation,
    execution,
    media,
    places,
    subjects,
)
from malbut_agent_server.application.fall_clip_planner import FallClipPlanner, RecordedClip
from malbut_agent_server.application.fall_cloud_association import associate_timed_finding
from malbut_agent_server.application.fall_deferred_association import (
    DeferredDiscovery,
    DiscoveryLinkResult,
)
from malbut_agent_server.application.fall_frame_buffer import FallFrameBuffer
from malbut_agent_server.application.fall_pending_association import (
    AssociationWait,
    PendingAssociation,
)
from malbut_agent_server.application.fall_people_recorder import FallPeopleRecorder
from malbut_agent_server.application.fall_scene_place import CameraMotionLog
from malbut_agent_server.application.fall_subject_evidence import FallSubjectEvidence
from malbut_agent_server.domain.fall_monitoring import (
    AgentCheckReply,
    CloudDiscovery,
    CloudPoseLink,
    FallCandidate,
    FallIncident,
    FallRuntimeEvent,
    FallRuntimePolicy,
    NotificationLevel,
    PersonObservation,
    RgbFrame,
    SubjectFrame,
    SubjectObservation,
    identifier,
    timestamp,
)
from malbut_agent_server.ports.cloud_fall import CloudFallProvider
from malbut_agent_server.ports.fall_event_journal import FallEventJournal, FallJournalError

__all__ = [
    'APPROACH_OUTCOMES',
    'CHECK_FAILURE_REASONS',
    'CLEARED_PLACE_MAX_S',
    'CLEARED_REASONS',
    'COMPARE_WAIT_S',
    'CloudAnalysisStatus',
    'CloudFallMonitor',
    'FIRST_SCENES_MAX',
    'FLOOR_ACTIVE_LOOK',
    'FLOOR_ACTIVE_REASON',
    'FURNITURE_HEIGHT_M',
    'GUARD_FAILURES',
    'NOT_CONFIRMED_REASONS',
    'OFF_FLOOR_LOOKS',
    'OFF_FLOOR_REASONS',
    'PERSON_POSE_WAIT_S',
    'POSTURE_GAP_S',
    'POSTURE_SETTLE_S',
    'PersonCheck',
    'RUNTIME_CLOUD_BLOCKS',
    'SAME_PERSON_GAP_S',
    'SAME_PERSON_INSIDE',
    'SAME_PERSON_IOU',
    'SAME_PLACE_CASE_S',
    'SPOT_CHECK_MAX_S',
    'STOP_REASONS',
    'SUPPORT_BAND',
    'VIEW_LIMIT_S',
    'WAITING_REASONS',
    '_inside',
    'associate_timed_finding',
]


class CloudFallMonitor:
    """One device boot; event-loop confined, with at most one Cloud task.

    All settings begin OFF, including consent. Snapshot access returns copies.
    Without a journal, events are only an in-memory test handoff. With a journal,
    incident metadata is committed before exposure; upload runs separately.
    """

    _LEVEL_ORDER = LEVEL_ORDER

    def __init__(
        self,
        *,
        device_id: str,
        boot_id: str,
        policy: FallRuntimePolicy,
        buffer: FallFrameBuffer,
        provider: CloudFallProvider,
        journal: Optional[FallEventJournal] = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        clip_planner: Optional[FallClipPlanner] = None,
        people_recorder: Optional[FallPeopleRecorder] = None,
        place_locator: Any = None,
        place_log: Optional[Callable[[str], None]] = None,
    ) -> None:
        identifier(device_id)
        identifier(boot_id)
        if provider.execution_target != 'cloud':
            raise ValueError('local VLM providers are not allowed')
        self.device_id, self.boot_id = device_id, boot_id
        self._place_log = place_log
        self.policy, self.buffer = policy, buffer
        self._provider, self._clock = provider, clock
        self._journal = journal
        self._storage_failed = False
        self._wall_clock = wall_clock
        self._clips = clip_planner or FallClipPlanner()
        self._clip_offsets: Dict[Any, float] = {}
        self._clip_ranges: deque[RecordedClip] = deque(maxlen=256)
        self.clip_storage_failures = 0
        self._people = people_recorder or FallPeopleRecorder(boot_id=boot_id)
        self._people_ready: deque[Any] = deque(maxlen=256)
        self.people_storage_failures = 0
        self._enabled = self._camera = self._consent = self._connected = False
        self._epoch = 0
        self._incidents: Dict[str, FallIncident] = {}
        self._questions: Dict[str, FallIncident] = {}
        self._settled_subjects: Dict[str, Tuple[str, str]] = {}
        self._events: List[FallRuntimeEvent] = []
        self._calls: deque[float] = deque()
        self._task: Optional[asyncio.Task[Any]] = None
        self._active_incident: Optional[str] = None
        self._running = False
        self._runtime_cloud_block: Optional[str] = None
        self._analysis_status = CloudAnalysisStatus()
        self._scan_anchor = 0.0
        self._person_observation: Optional[PersonObservation] = None
        self._last_person_seen: Optional[float] = None
        self._last_scan_end = -1.0
        self._subject_evidence = FallSubjectEvidence(
            retention_s=buffer.retention_s, max_frames=buffer.max_frames,
        )
        self._discoveries: 'OrderedDict[str, DeferredDiscovery]' = OrderedDict()
        self._association_wait = AssociationWait()
        self._place = place_locator
        self._camera_motion = CameraMotionLog()
        self.max_open_scene_cases = 3
        self.approach_enabled = False
        self.full_view_enabled = False
        self._running_missions: Tuple[str, ...] = ()
        self._person_checks: Dict[str, PersonCheck] = {}
        self._spot_checks: Dict[str, Tuple[Any, float]] = {}
        self._cleared_places: Dict[str, Tuple[Any, float]] = {}
        self._case_places: Dict[str, Any] = {}
        self._legacy_levels: Dict[str, NotificationLevel] = {}
        self._sent_notices: Dict[str, Set[str]] = {}
        self._first_scenes: 'OrderedDict[str, Tuple[Any, Any]]' = OrderedDict()
        self._comparisons: deque[Tuple[Any, ...]] = deque()
        self._guardian_closing: Set[str] = set()
        self._same_person: Dict[str, str] = {}

    def _now(self) -> float:
        value = self._clock()
        timestamp(value)
        return value

    def _emit(
        self, kind: str, incident: Optional[FallIncident] = None, **kwargs: Any,
    ) -> None:
        if self._storage_failed:
            raise FallJournalError('recover journal and restart monitor before emitting')
        event = FallRuntimeEvent(
            event_id=str(uuid4()),
            kind=kind,
            incident_id=incident.incident_id if incident else None,
            subject_key=incident.subject_key if incident else None,
            confirmation_scope=(
                'scene' if incident and incident.subject_key is None else 'subject'
            ),
            evidence_revision=incident.revision if incident else None,
            **kwargs,
        )
        if self._journal is not None and (incident is not None or event.discovery is not None):
            try:
                if event.discovery is not None:
                    self._journal.append_discovery(
                        device_id=self.device_id, boot_id=self.boot_id, event=event,
                    )
                else:
                    assert incident is not None
                    self._journal.append(
                        device_id=self.device_id, boot_id=self.boot_id,
                        event=event, incident=replace(incident),
                    )
            except Exception:
                self._storage_failed = True
                self.configure(
                    enabled=False, camera_enabled=False,
                    cloud_consent=False, connected=False,
                )
                raise FallJournalError('fall event persistence failed') from None
        self._events.append(event)

    def drain_events(self) -> Tuple[FallRuntimeEvent, ...]:
        """Caller must persist/route these; this is not a delivery receipt."""
        events, self._events = tuple(self._events), []
        return events

    def drain_clip_ranges(self) -> Tuple[RecordedClip, ...]:
        """Recorded clip ranges since the last drain; not an upload receipt."""
        ranges = tuple(self._clip_ranges)
        self._clip_ranges.clear()
        return ranges

    def drain_people(self) -> Tuple[Any, ...]:
        """Finalized person boxes since the last drain; not an upload receipt."""
        ready = tuple(self._people_ready)
        self._people_ready.clear()
        return ready

    def incident(self, incident_id: str) -> FallIncident:
        return replace(self._incidents[incident_id])

    @property
    def analysis_status(self) -> CloudAnalysisStatus:
        if (self._analysis_status.state == 'cancel_requested'
                and self._task is not None and self._task.done()):
            self._analysis_status = replace(self._analysis_status, state='canceled')
        return self._analysis_status

    def _cancel_analysis(self) -> None:
        if self._task is not None and not self._task.done():
            self._analysis_status = replace(
                self._analysis_status, state='cancel_requested', last_error_code='',
            )
            self._task.cancel()

    def set_cloud_block(self, reason: Optional[str]) -> None:
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
            self._scan_anchor = self._now()

    def configure(
        self,
        *,
        enabled: bool,
        camera_enabled: bool,
        cloud_consent: bool,
        connected: bool,
    ) -> None:
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

    def ingest_rgb(self, frame: RgbFrame) -> bool:
        if not self._enabled or not self._camera:
            return False
        if frame.captured_at > self._now():
            raise ValueError('future RGB timestamp')
        self.buffer.append(frame)
        return True

    def set_running_missions(self, capability_ids: Sequence[str]) -> None:
        """Manager missions now; recorded when a check interrupts one."""
        self._running_missions = tuple(capability_ids)

    def _raise_level(self, incident: FallIncident, level: NotificationLevel) -> None:
        previous = incident.notification_level
        if previous is None or self._LEVEL_ORDER[previous] < self._LEVEL_ORDER[level]:
            incident.notification_level = level

    def _notify(
        self, incident: FallIncident, level: NotificationLevel, reason: str,
    ) -> None:
        previous = self._legacy_levels.get(incident.incident_id)
        if previous is not None and self._LEVEL_ORDER[previous] >= self._LEVEL_ORDER[level]:
            return
        self._legacy_levels[incident.incident_id] = level
        self._raise_level(incident, level)
        self._emit('notification_requested', incident, reason=reason, notification_level=level)

    def _notify_once(
        self, incident: FallIncident, level: NotificationLevel, reason: str,
    ) -> None:
        sent = self._sent_notices.setdefault(incident.incident_id, set())
        if reason in sent:
            return
        sent.add(reason)
        self._raise_level(incident, level)
        self._emit('notification_requested', incident, reason=reason, notification_level=level)

    # ------------------------------------------------------------ media & overlays

    def _people_call(self, method: Callable[..., Any], *args: Any) -> None:
        media.people_call(self, method, *args)

    def flush_people(self) -> None:
        """Persist segments whose boxes are complete; never stops fall handling."""
        media.flush_people(self)

    def _record_cloud_boxes(
        self, incident: FallIncident, times: Sequence[float], finding: Any,
    ) -> None:
        media.record_cloud_boxes(self, incident, times, finding)

    def _record_clip(
        self, incident: FallIncident, start: float, end: float, *,
        anchor_kind: str, found_down: bool = False,
    ) -> None:
        media.record_clip(self, incident, start, end, anchor_kind=anchor_kind,
                          found_down=found_down)

    def _record_window_clip(self, incident: FallIncident, request: Any, finding: Any) -> None:
        media.record_window_clip(self, incident, request, finding)

    def _record_candidate_clip(self, incident: FallIncident, candidate: FallCandidate) -> None:
        media.record_candidate_clip(self, incident, candidate)

    # ------------------------------------------------------------ subjects & candidates

    def observe_person(self, observation: PersonObservation) -> bool:
        return subjects.observe_person(self, observation)

    def periodic_interval_s(self) -> float:
        return subjects.periodic_interval_s(self)

    def observe_subject(self, observation: SubjectObservation) -> bool:
        return subjects.observe_subject(self, observation)

    def invalidate_subject_input(self) -> None:
        subjects.invalidate_subject_input(self)

    def ingest_subject_frame(self, frame: SubjectFrame) -> bool:
        return subjects.ingest_subject_frame(self, frame)

    def _remember_settled_subject(self, incident: FallIncident) -> None:
        subjects.remember_settled_subject(self, incident)

    def _refresh_settled_subjects(self) -> None:
        subjects.refresh_settled_subjects(self)

    def _settled_subject(self, key: str, token: str, finding: Any) -> Optional[FallIncident]:
        return subjects.settled_subject(self, key, token, finding)

    def _refresh_subject(self, incident: FallIncident) -> None:
        subjects.refresh_subject(self, incident)

    def candidate(self, candidate: FallCandidate) -> Optional[str]:
        return subjects.handle_candidate(self, candidate)

    # ------------------------------------------------------------ confirmation & closure

    def _pending_question_id(self, incident: FallIncident) -> Optional[str]:
        return confirmation.pending_question_id(self, incident)

    def ask_question(self, incident_id: str) -> Optional[str]:
        return confirmation.ask_question(self, incident_id)

    def _continue_confirmation(self, incident: FallIncident) -> None:
        confirmation.continue_confirmation(self, incident)

    def confirmation_result(
        self, *, incident_id: str, question_id: str, subject_key: Optional[str],
        evidence_revision: int, situation_assessment: str, help_needed: bool,
    ) -> bool:
        return confirmation.confirmation_result(
            self, incident_id=incident_id, question_id=question_id,
            subject_key=subject_key, evidence_revision=evidence_revision,
            situation_assessment=situation_assessment, help_needed=help_needed,
        )

    def confirmation_failed(
        self, *, incident_id: str, question_id: str, evidence_revision: int,
        reason: str = 'question_error',
    ) -> bool:
        return confirmation.confirmation_failed(
            self, incident_id=incident_id, question_id=question_id,
            evidence_revision=evidence_revision, reason=reason,
        )

    def pending_questions(self) -> Tuple[FallRuntimeEvent, ...]:
        return confirmation.pending_questions(self)

    def agent_reply(self, reply: AgentCheckReply) -> bool:
        return confirmation.agent_reply(self, reply)

    def _decision_needed(self, incident: FallIncident) -> None:
        confirmation.decision_needed(self, incident)

    def request_recheck(self, incident_id: str) -> bool:
        return confirmation.request_recheck(self, incident_id)

    def mark_unresolved(
        self, incident_id: str, *, revision: int, suspicion_persists: bool,
    ) -> None:
        confirmation.mark_unresolved(
            self, incident_id, revision=revision, suspicion_persists=suspicion_persists,
        )

    def resolve(self, incident_id: str, *, revision: int, reason: str) -> None:
        confirmation.resolve(self, incident_id, revision=revision, reason=reason)

    def close_by_guardian(self, incident_id: str) -> bool:
        return confirmation.close_by_guardian(self, incident_id)

    def _close_deferred(self) -> None:
        confirmation.close_deferred(self)

    # ------------------------------------------------------------ approach & posture

    def _approach_target(self, incident: FallIncident) -> Any:
        return approach.approach_target(self, incident)

    def _spot(self, incident: FallIncident) -> Any:
        return approach.spot(self, incident)

    def _remember_first_scene(self, incident: FallIncident) -> None:
        approach.remember_first_scene(self, incident)

    def approach_result(
        self, *, incident_id: str, question_id: str, evidence_revision: int, outcome: str,
    ) -> bool:
        return approach.approach_result(
            self, incident_id=incident_id, question_id=question_id,
            evidence_revision=evidence_revision, outcome=outcome,
        )

    def return_result(
        self, *, incident_id: str, question_id: str, evidence_revision: int, outcome: str,
    ) -> bool:
        return approach.return_result(
            self, incident_id=incident_id, question_id=question_id,
            evidence_revision=evidence_revision, outcome=outcome,
        )

    def check_waiting(
        self, *, incident_id: str, question_id: str, evidence_revision: int, reason: str,
    ) -> bool:
        return approach.check_waiting(
            self, incident_id=incident_id, question_id=question_id,
            evidence_revision=evidence_revision, reason=reason,
        )

    def check_not_confirmed(
        self, *, incident_id: str, question_id: str, evidence_revision: int, reason: str,
    ) -> bool:
        return approach.check_not_confirmed(
            self, incident_id=incident_id, question_id=question_id,
            evidence_revision=evidence_revision, reason=reason,
        )

    def check_stopped(
        self, *, incident_id: str, question_id: str, evidence_revision: int, reason: str,
    ) -> bool:
        return approach.check_stopped(
            self, incident_id=incident_id, question_id=question_id,
            evidence_revision=evidence_revision, reason=reason,
        )

    def revisit_started(
        self, *, incident_id: str, question_id: str, evidence_revision: int, reason: str,
    ) -> bool:
        return approach.revisit_started(
            self, incident_id=incident_id, question_id=question_id,
            evidence_revision=evidence_revision, reason=reason,
        )

    def revisit_result(
        self, *, incident_id: str, question_id: str, evidence_revision: int, outcome: str,
    ) -> bool:
        return approach.revisit_result(
            self, incident_id=incident_id, question_id=question_id,
            evidence_revision=evidence_revision, outcome=outcome,
        )

    def view_result(
        self, *, incident_id: str, question_id: str, evidence_revision: int, outcome: str,
    ) -> bool:
        return approach.view_result(
            self, incident_id=incident_id, question_id=question_id,
            evidence_revision=evidence_revision, outcome=outcome,
        )

    def _advance_person_checks(self) -> None:
        approach.advance_person_checks(self)

    def _expire_comparisons(self) -> None:
        approach.expire_comparisons(self)

    async def _cloud_same_person(
        self, iid: str, question_id: str, off_floor: bool, first: Any, after: Any, queued: float,
    ) -> bool:
        return await approach.cloud_same_person(
            self, iid, question_id, off_floor, first, after, queued,
        )

    async def _cloud_posture_check(self, iid: str, check: PersonCheck) -> bool:
        return await approach.cloud_posture_check(self, iid, check)

    async def _cloud_person_check(self, iid: str, check: PersonCheck) -> bool:
        return await approach.cloud_person_check(self, iid, check)

    # ------------------------------------------------------------ places & geometry

    def _spot_under_check(self, subject_key: str) -> Optional[str]:
        return places.spot_under_check(self, subject_key)

    def _subject_places(self, subject_key: Optional[str]) -> Tuple[Any, ...]:
        return places.subject_places(self, subject_key)

    def _remember_case_place(self, incident: FallIncident) -> None:
        places.remember_case_place(self, incident)

    def _incident_place(self, incident: FallIncident) -> Any:
        return places.incident_place(self, incident)

    def _remember_cleared_place(self, incident: FallIncident) -> None:
        places.remember_cleared_place(self, incident)

    def _cleared_place(self, points: Sequence[Any]) -> Optional[str]:
        return places.cleared_place(self, points)

    def _forget_cleared_places(self, points: Sequence[Any]) -> None:
        places.forget_cleared_places(self, points)

    def _refresh_cleared_places(self, frame: SubjectFrame) -> None:
        places.refresh_cleared_places(self, frame)

    def _held_by_place(self, times: Sequence[float], finding: Any) -> Optional[str]:
        return places.held_by_place(self, times, finding)

    def _same_person_incident(self, candidate: FallCandidate) -> Optional[FallIncident]:
        return places.same_person_incident(self, candidate)

    def _map_points(self, times: Sequence[float], finding: Any) -> Tuple[Any, ...]:
        return places.map_points(self, times, finding)

    def _on_furniture(self, times: Sequence[float], finding: Any) -> bool:
        return places.on_furniture(self, times, finding)

    def _log_pose_place(self, incident: FallIncident) -> None:
        places.log_pose_place(self, incident)

    def _scene_case_for(
        self, times: Sequence[float], boxes: Sequence[Any],
        points: Sequence[Any], request_id: Optional[str],
    ) -> Optional[FallIncident]:
        return places.scene_case_for(self, times, boxes, points, request_id)

    # ------------------------------------------------------------ associations & tracking

    def _scene_incident_versions(self) -> Dict[str, Any]:
        return associations.scene_incident_versions(self)

    def _record_crosscheck(
        self, request: Any, reply: Any, snapshot: Any, versions: Dict[str, Any], *,
        pose_snapshot: Any = None, pose_generation: Optional[int] = None,
    ) -> None:
        associations.record_crosscheck(
            self, request, reply, snapshot, versions,
            pose_snapshot=pose_snapshot, pose_generation=pose_generation,
            associate_finding=associate_timed_finding,
        )

    def _cancel_pending_associations(self) -> None:
        associations.cancel_pending_associations(self)

    def _finish_pending_association(
        self, pending: PendingAssociation, status: str, reason: str, *,
        incident_id: Optional[str] = None, subject_key: Optional[str] = None, proof: Any = None,
    ) -> CloudDiscovery:
        return associations.finish_pending_association(
            self, pending, status, reason,
            incident_id=incident_id, subject_key=subject_key, proof=proof,
        )

    def _retry_pending_associations(self) -> None:
        associations.retry_pending_associations(
            self, associate_finding=associate_timed_finding,
        )

    def _release_pending_associations(self) -> None:
        associations.release_pending_associations(self)

    def maintain_associations(self) -> None:
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

    def _pose_link_proof(
        self, source_id: Any, revision: int, token: str,
        times: Sequence[float], finding: Any, measured: Any,
    ) -> CloudPoseLink:
        return associations.pose_link_proof(
            self, source_id, revision, token, times, finding, measured,
        )

    def _retry_pose_associations(self) -> None:
        associations.retry_pose_associations(
            self, associate_finding=associate_timed_finding,
        )

    def begin_discovery_tracking(self, discovery_id: str) -> str:
        return associations.begin_discovery_tracking(self, discovery_id)

    def tracking_session_reason(self, session_id: str) -> Optional[str]:
        return associations.tracking_session_reason(self, session_id)

    def end_discovery_tracking(self, session_id: str) -> None:
        associations.end_discovery_tracking(self, session_id)

    def pose_observation_ready(self, observed_at: float) -> bool:
        return self._subject_evidence.observed_through(observed_at)

    def ingest_discovery_track(
        self, session_id: str, *, observed_at: float, box: Any,
    ) -> DiscoveryLinkResult:
        return associations.ingest_discovery_track(
            self, session_id, observed_at=observed_at, box=box,
        )

    def _make_room(self) -> bool:
        return associations.make_room(self)

    # ------------------------------------------------------------ cloud execution

    def _cloud_block(self, now: float) -> Optional[str]:
        return execution.cloud_block(self, now)

    async def run_once(self) -> bool:
        return await execution.run_once(self)

    def _failure(
        self, incident: Optional[FallIncident], reason: str, *, request: Any = None,
    ) -> None:
        execution.handle_failure(self, incident, reason, request=request)

    def _retry_or_ask(self, incident: FallIncident, reason: str) -> None:
        execution.retry_or_ask(self, incident, reason)

    @staticmethod
    def _consume_exception(task: asyncio.Task[Any]) -> None:
        execution.consume_exception(task)

    async def close(self) -> None:
        """Request cancellation; a broken adapter may still require termination."""
        self.configure(
            enabled=False, camera_enabled=False,
            cloud_consent=False, connected=False,
        )
        if self._task is not None:
            self._task.cancel()
