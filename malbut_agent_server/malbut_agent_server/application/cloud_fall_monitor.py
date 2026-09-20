"""Cloud-only fall workflow core, driven by an owning asyncio event loop.

Adapters ingest RGB/candidates and deliver events to speech/notification sinks.
This module never infers a spoken answer, drives the robot, sends a guardian
message, or imports the evaluation runner. Only the approved narrow normal
closure rule is automatic; other closure decisions remain explicit.
"""

import asyncio
from collections import deque
from dataclasses import replace
import time
from typing import Callable, Optional
from uuid import uuid4

from malbut_agent_server.application.fall_frame_buffer import FallFrameBuffer
from malbut_agent_server.application.fall_normal_closure import (
    normal_closure_supported, normal_path_ready,
)
from malbut_agent_server.application.fall_subject_evidence import FallSubjectEvidence
from malbut_agent_server.application.fall_cloud_association import associate_finding
from malbut_agent_server.domain.fall_monitoring import (
    AgentCheckReply, CloudFallReply, CloudFallRequest, FallCandidate, FallIncident,
    FallRuntimeEvent, FallRuntimePolicy, IncidentState, RgbFrame,
    NotificationLevel, PersonObservation, PersonVisibility,
    NormalVideoCheck, SubjectCheckState, SubjectObservation, SubjectFrame,
    VideoAssessment, VoiceAnswer, identifier, timestamp,
    CandidateKind, CloudDiscovery, CloudPersonFinding,
)
from malbut_agent_server.ports.cloud_fall import CloudFallProvider, CloudFallProviderError
from malbut_agent_server.ports.fall_event_journal import FallEventJournal, FallJournalError


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
                 clock: Callable[[], float] = time.monotonic) -> None:
        identifier(device_id)
        identifier(boot_id)
        if provider.execution_target != 'cloud':
            raise ValueError('local VLM providers are not allowed')
        self.device_id, self.boot_id = device_id, boot_id
        self.policy, self.buffer = policy, buffer
        self._provider, self._clock = provider, clock
        self._journal = journal
        self._storage_failed = False
        self._enabled = self._camera = self._consent = self._connected = False
        self._epoch = 0
        self._incidents = {}
        self._events = []
        self._calls = deque()
        self._task: Optional[asyncio.Task] = None
        self._active_incident: Optional[str] = None
        self._running = False
        self._scan_anchor = 0.0
        self._person_observation: Optional[PersonObservation] = None
        self._last_person_seen: Optional[float] = None
        self._last_scan_end = -1.0
        self._subject_evidence = FallSubjectEvidence(
            retention_s=buffer.retention_s, max_frames=buffer.max_frames)

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

    def incident(self, incident_id: str) -> FallIncident:
        return replace(self._incidents[incident_id])

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
        self._enabled, self._camera, self._consent, self._connected = values
        if not all(values) and self._task is not None:
            self._task.cancel()
        if not enabled or not camera_enabled:
            self.buffer.clear()
            self._subject_evidence.clear()
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
        self._subject_evidence.active = True
        self._subject_evidence.clear()
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
        return True

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
        current = next((i for i in self._incidents.values()
                        if i.subject_key == candidate.subject_key
                        and i.state is not IncidentState.RESOLVED), None)
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
                current.question_id = None
                self.ask_question(current.incident_id)
            return current.incident_id
        if len(self._incidents) >= self.policy.max_incidents:
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
        self._emit('incident_opened', current)
        self.ask_question(current.incident_id)
        return current.incident_id

    def ask_question(self, incident_id: str) -> Optional[str]:
        incident = self._incidents[incident_id]
        if incident.state is IncidentState.RESOLVED:
            return None
        if incident.question_id is not None and incident.answer is None:
            return incident.question_id
        incident.question_id = str(uuid4())
        incident.answer = None
        incident.answer_question_played = False
        self._emit('question_requested', incident,
                   question_id=incident.question_id)
        return incident.question_id

    def agent_reply(self, reply: AgentCheckReply) -> bool:
        """Agent owns playback, waiting, speaker association and answer interpretation."""
        if not isinstance(reply, AgentCheckReply):
            raise ValueError('invalid Agent reply')
        incident = self._incidents.get(reply.incident_id)
        if (incident is None or incident.question_id != reply.question_id
                or incident.subject_key != reply.subject_key
                or incident.revision != reply.evidence_revision
                or incident.state is IncidentState.RESOLVED):
            return False
        answer = reply.answer
        # A help request can supersede an earlier answer to this question.
        if incident.answer is not None and answer is not VoiceAnswer.HELP:
            return False
        incident.answer = answer
        incident.answer_question_played = reply.question_played
        if answer is VoiceAnswer.HELP:
            incident.state = IncidentState.HELP_REQUIRED
            self._notify(incident, NotificationLevel.URGENT, 'help_requested')
        else:
            if answer is VoiceAnswer.NO_RESPONSE:
                self._notify(incident, NotificationLevel.CHECK, 'person_no_response')
            if answer is VoiceAnswer.FAILED:
                self._emit('agent_check_failed', incident, reason='agent_failed')
        self._emit('voice_result', incident, reason=answer.value)
        if answer is not VoiceAnswer.HELP:
            self._decision_needed(incident)
        return True

    def _notify(self, incident: FallIncident, level: NotificationLevel, reason: str) -> None:
        order = {NotificationLevel.INFO: 1, NotificationLevel.CHECK: 2,
                 NotificationLevel.URGENT: 3}
        previous = incident.notification_level
        if previous is not None and order[previous] >= order[level]:
            return
        incident.notification_level = level
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
        if reason not in {'normal_verified', 'risk_cleared', 'response_completed'}:
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

    def _record_crosscheck(self, request, reply, snapshot, versions):
        if reply.assessment not in (
            VideoAssessment.OBSERVED_FALL, VideoAssessment.SUSPECTED_FALL,
        ):
            return  # A scene-level normal result never clears a person's case.
        findings = reply.findings or (CloudPersonFinding(
            reply.assessment, CandidateKind.MOTION_SEEN
            if reply.assessment is VideoAssessment.OBSERVED_FALL else CandidateKind.UNKNOWN),)
        # Strongest evidence first if a provider accidentally repeats a person.
        order = sorted(enumerate(findings), key=lambda pair:
                       pair[1].assessment is not VideoAssessment.OBSERVED_FALL)
        linked = {}
        for index, finding in order:
            match = associate_finding(finding, snapshot)
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
                    if before != after:
                        reason = 'incident_changed_during_scan'
                    else:
                        current = next((i for i in self._incidents.values()
                                        if i.subject_key == match.subject_key
                                        and i.state is not IncidentState.RESOLVED), None)
                        if current and current.subject_association_token != match.token:
                            reason = 'incident_target_continuity_unverified'
                        else:
                            iid = self._merge_cloud_person(
                                request, finding, match.subject_key, match.token)
                            if iid is None:
                                reason = 'incident_capacity'
                            else:
                                linked[match.subject_key] = iid
            discovery = CloudDiscovery(
                str(uuid4()), request.request_id, index, finding,
                tuple(f.captured_at for f in request.window.frames), reason,
                match.subject_key if iid else None, iid)
            # Unidentified suspicion is durable independently of incident capacity.
            # No guessed person, synthetic answer, or automatic targeted question.
            self._emit('cloud_discovery', reason=reason, discovery=discovery)

    def _merge_cloud_person(self, request, finding, subject_key, token):
        current = next((i for i in self._incidents.values() if i.subject_key == subject_key
                        and i.state is not IncidentState.RESOLVED), None)
        observed_at = request.window.frames[finding.regions[-1].frame_index].captured_at
        end = request.window.frames[-1].captured_at
        new = current is None
        if new:
            if len(self._incidents) >= self.policy.max_incidents:
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
            previous = current.video.assessment if current.video else None
            escalation = previous not in (VideoAssessment.OBSERVED_FALL,
                                          VideoAssessment.SUSPECTED_FALL) or (
                previous is VideoAssessment.SUSPECTED_FALL
                and finding.assessment is VideoAssessment.OBSERVED_FALL)
            if escalation and current.answer is not VoiceAnswer.HELP:
                current.revision += 1
                current.question_id = None
                current.answer = None
                current.answer_question_played = False
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
            self._emit('incident_opened', current)
        self._emit('analysis_completed', current, request=request, reply=current.video)
        if current.question_id is None:
            self.ask_question(current.incident_id)
        self._decision_needed(current)
        return current.incident_id

    async def run_once(self) -> bool:
        """One attempt/scan; queued normal rechecks obey the existing budget."""
        if self._running or (self._task is not None and not self._task.done()):
            return False
        self._running = True
        try:
            return await self._run_once()
        finally:
            self._running = False
            self._active_incident = None

    async def _run_once(self) -> bool:
        now = self._now()
        if not self._enabled or not self._camera:
            return False
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
        scene_versions = self._scene_incident_versions() if incident is None else {}
        if incident:
            incident.last_window_end = window.frames[-1].captured_at
        else:
            self._last_scan_end = window.frames[-1].captured_at
        epoch = self._epoch
        self._active_incident = request.incident_id
        self._calls.append(now)
        self._task = None
        try:
            self._task = asyncio.create_task(self._provider.analyze(request))
            self._task.add_done_callback(self._consume_exception)
            done, _ = await asyncio.wait({self._task}, timeout=self.policy.cloud_timeout_s)
            if not done:
                self._task.cancel()
                self._failure(incident, 'cloud_timeout')
                return True
            if self._epoch != epoch or self._task.cancelled():
                self._failure(incident, 'cloud_permission_changed')
                return True
            reply = self._task.result()
            if not isinstance(reply, CloudFallReply):
                raise ValueError('invalid reply contract')
        except asyncio.CancelledError:
            if self._task is not None:
                self._task.cancel()
            self._failure(incident, 'worker_cancelled')
            raise
        except CloudFallProviderError as error:
            self._failure(incident, error.code)
            return True
        except Exception:
            self._failure(incident, 'cloud_failed_or_invalid_response')
            return True
        finally:
            self._active_incident = None
        if incident:
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
            self._emit('analysis_completed', incident, request=request, reply=reply)
            self._decision_needed(incident)
        else:
            self._record_crosscheck(request, reply, scene_snapshot, scene_versions)
            self._emit('crosscheck_completed', request=request, reply=reply)
        return True

    def _failure(self, incident: Optional[FallIncident], reason: str) -> None:
        if incident:
            incident.last_failure = reason
            # Earlier normal must not stand in for a failed new attempt.
            incident.video = None
            incident.video_revision = None
            incident.normal_checks = ()
            incident.subject_observation = None
            self._active_incident = None
            self._emit('analysis_unavailable', incident, reason=reason)
            self._decision_needed(incident)
        else:
            self._emit('crosscheck_skipped', reason=reason)

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
