"""Subject observation ingestion, settled-episode tracking, and Pose candidates."""

from typing import Any, Optional
from uuid import uuid4

from malbut_agent_server.application.fall_normal_closure import normal_closure_supported
from malbut_agent_server.domain.fall_monitoring import (
    CandidateKind,
    FallCandidate,
    FallIncident,
    IncidentState,
    PersonObservation,
    PersonVisibility,
    SubjectCheckState,
    SubjectFrame,
    SubjectObservation,
    VideoAssessment,
    VoiceAnswer,
)


def observe_person(monitor: Any, observation: PersonObservation) -> bool:
    """Healthy negative detections reduce frequency, never disable scanning."""
    if not isinstance(observation, PersonObservation):
        raise ValueError('invalid person observation')
    now = monitor._now()
    if observation.observed_at > now:
        raise ValueError('future person observation')
    if not monitor._enabled or not monitor._camera:
        return False
    if now - observation.observed_at > monitor.policy.max_person_observation_age_s:
        return False
    previous = monitor._person_observation
    if previous is not None and observation.observed_at <= previous.observed_at:
        return False
    monitor._person_observation = observation
    if observation.visibility is PersonVisibility.SEEN:
        monitor._last_person_seen = observation.observed_at
    else:
        # Losing detector health/visibility after a subject check cannot
        # leave that check usable for a later automatic closure.
        for incident in monitor._incidents.values():
            subject = incident.subject_observation
            if subject is not None and observation.observed_at >= subject.observed_at:
                incident.subject_observation = None
    return True


def periodic_interval_s(monitor: Any) -> float:
    """Missing/stale/failed detector input is unknown, not an empty room."""
    now = monitor._now()
    observation = monitor._person_observation
    fast = monitor.policy.scan_interval_s
    if any(i.state is not IncidentState.RESOLVED for i in monitor._incidents.values()):
        return fast
    if (observation is None
            or now - observation.observed_at > monitor.policy.max_person_observation_age_s
            or observation.visibility is not PersonVisibility.NOT_SEEN):
        return fast
    if (monitor._last_person_seen is not None
            and now - monitor._last_person_seen < monitor.policy.person_hold_s):
        return fast
    return monitor.policy.idle_scan_interval_s


def observe_subject(monitor: Any, observation: SubjectObservation) -> bool:
    """Explicit association/observation boundary, not scene-level presence.

    There is deliberately no conversion from empty candidate arrays into
    CLEAR. The supplying detector/association module must verify it.
    """
    if not isinstance(observation, SubjectObservation):
        raise ValueError('invalid subject observation')
    now = monitor._now()
    if observation.observed_at > now:
        raise ValueError('future subject observation')
    incident = monitor._incidents.get(observation.incident_id)
    if (not monitor._enabled or not monitor._camera or incident is None
            or incident.state is IncidentState.RESOLVED
            or observation.subject_key != incident.subject_key
            or observation.evidence_revision != incident.revision
            or not incident.normal_checks
            or observation.request_id != incident.normal_checks[-1].request_id
            or now - observation.observed_at > monitor.policy.max_person_observation_age_s
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
    monitor._decision_needed(incident)
    return True


def invalidate_subject_input(monitor: Any) -> None:
    """Reset Pose track evidence and continuity tokens after detector reset."""
    monitor._camera_motion.unknown(monitor._now())
    monitor._subject_evidence.active = True
    monitor._subject_evidence.clear()
    monitor._settled_subjects.clear()
    monitor._same_person.clear()
    monitor._association_wait.anchors.clear()
    monitor._association_wait.stationary_at = None
    for entry in monitor._discoveries.values():
        if entry.track is not None:
            entry.track.broken = True
    for incident in monitor._incidents.values():
        incident.subject_observation = None


def ingest_subject_frame(monitor: Any, frame: SubjectFrame) -> bool:
    """Ingest one Pose frame and update settled/cleared/association state."""
    if not isinstance(frame, SubjectFrame):
        raise ValueError('invalid subject frame')
    now = monitor._now()
    if frame.observed_at > now:
        raise ValueError('future subject frame')
    if (not monitor._enabled or not monitor._camera
            or now - frame.observed_at > monitor.policy.max_person_observation_age_s):
        return False
    monitor._subject_evidence.append(frame)
    monitor._camera_motion.observe(frame.observed_at, moving=frame.camera_moving)
    refresh_settled_subjects(monitor)
    monitor._refresh_cleared_places(frame)
    monitor._people_call(monitor._people.observe, frame.observed_at, tuple(
        (p.subject_key, p.box, p.strong) for p in frame.subjects if p.box is not None
    ))
    for incident in monitor._incidents.values():
        if incident.state is IncidentState.RESOLVED:
            continue
        if incident.subject_association_token is None:
            incident.subject_association_token = monitor._subject_evidence.token_at(
                incident.subject_key, incident.last_observed_at,
            )
        refresh_subject(monitor, incident)
        # Do not emit a decision event for every camera frame.
        if normal_closure_supported(
                incident, now=now,
                max_observation_age_s=monitor.policy.max_person_observation_age_s):
            monitor._decision_needed(incident)
    monitor._retry_pose_associations()
    monitor._association_wait.observe(frame, monitor._incidents, monitor._subject_evidence)
    monitor._retry_pending_associations()
    return True


def remember_settled_subject(monitor: Any, incident: FallIncident) -> None:
    """Guard user-cleared, still-low posture, not a timer-based cooldown."""
    if (incident.subject_key is None or incident.state is not IncidentState.RESOLVED
            or incident.answer is not VoiceAnswer.OKAY):
        return
    latest = monitor._subject_evidence.latest(incident.subject_key)
    if (latest is None or not latest[1]
            or latest[1] != incident.subject_association_token
            or monitor._now() - latest[0] > monitor.policy.max_person_observation_age_s
            or latest[2].state is not SubjectCheckState.SUSPECTED):
        return
    monitor._settled_subjects[incident.subject_key] = (latest[1], incident.incident_id)


def refresh_settled_subjects(monitor: Any) -> None:
    """Release settled-subject guards once the person stands up or leaves."""
    for key, (token, _) in tuple(monitor._settled_subjects.items()):
        latest = monitor._subject_evidence.latest(key)
        if (latest is None or latest[1] != token
                or latest[2].state is SubjectCheckState.CLEAR):
            # CLEAR is the detector's sustained upright observation, not
            # merely a missing box or a scene-wide normal VLM result.
            del monitor._settled_subjects[key]


def settled_subject(
    monitor: Any,
    key: str,
    token: str,
    finding: Any,
) -> Optional[FallIncident]:
    """Return the resolved incident if this finding continues a settled episode."""
    saved = monitor._settled_subjects.get(key)
    if (saved is None or saved[0] != token
            or finding.kind is not CandidateKind.ALREADY_DOWN
            or finding.assessment is not VideoAssessment.SUSPECTED_FALL):
        return None
    latest = monitor._subject_evidence.latest(key)
    if (latest is None or latest[1] != token
            or monitor._now() - latest[0] > monitor.policy.max_person_observation_age_s
            or latest[2].state is not SubjectCheckState.SUSPECTED):
        return None
    incident = monitor._incidents[saved[1]]
    return incident if incident.state is IncidentState.RESOLVED else None


def refresh_subject(monitor: Any, incident: FallIncident) -> None:
    """Derive a trusted subject CLEAR observation from continuous Pose frames."""
    if not monitor._subject_evidence.active:
        return  # Explicit trusted observation boundary remains available.
    incident.subject_observation = None
    checks = incident.normal_checks
    latest = monitor._subject_evidence.latest(incident.subject_key)
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
    assert incident.subject_key is not None
    incident.subject_observation = SubjectObservation(
        incident.incident_id, incident.subject_key, incident.revision,
        checks[-1].request_id, stamp, pose.state, True,
    )


def handle_candidate(monitor: Any, candidate: FallCandidate) -> Optional[str]:
    """Subject association is the caller's job, never a VLM identity."""
    if not isinstance(candidate, FallCandidate):
        raise ValueError('invalid candidate')
    now = monitor._now()
    if candidate.observed_at > now:
        raise ValueError('future candidate')
    if not monitor._enabled or not monitor._camera:
        return None
    if now - candidate.observed_at > monitor.policy.max_frame_age_s:
        monitor._emit('candidate_rejected', reason='stale_candidate')
        return None
    display_reason = (
        candidate.pose_reason or 'pose_candidate'
        if candidate.source == 'yolo_pose' else 'cloud_crosscheck'
    )
    if candidate.kind is CandidateKind.MOTION_SEEN:
        # A newly measured fall must not inherit an earlier resting answer.
        monitor._settled_subjects.pop(candidate.subject_key, None)
        monitor._forget_cleared_places(monitor._subject_places(candidate.subject_key))
    current = next((
        i for i in monitor._incidents.values()
        if i.subject_key == candidate.subject_key
        and i.state is not IncidentState.RESOLVED
    ), None)
    if current is None:
        current = monitor._same_person_incident(candidate)
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
        monitor._remember_case_place(current)
        monitor._record_candidate_clip(current, candidate)
        # Any fresh suspicion invalidates earlier clearance, even if it
        # does not warrant a new question/evidence revision.
        current.normal_checks = ()
        current.subject_observation = None
        current.normal_evidence_after = candidate.observed_at
        if candidate.significant_change:
            current.revision += 1
            current.kind = candidate.kind
            monitor.request_recheck(current.incident_id)
            # Old answers cannot describe the newly observed change.
            current.question_id = monitor._pending_question_id(current)
            current.answer = None
            current.situation_assessment = None
            current.help_needed = None
            monitor._emit('incident_updated', current, reason=display_reason)
        return current.incident_id
    if (candidate.kind is not CandidateKind.MOTION_SEEN
            and monitor._spot_under_check(candidate.subject_key) is not None):
        # Its close look decides. The detector sends the candidate again,
        # so a person still there after the check opens an incident then.
        monitor._emit('candidate_rejected', reason='spot_under_check')
        return None
    if (candidate.kind is not CandidateKind.MOTION_SEEN
            and monitor._cleared_place(monitor._subject_places(candidate.subject_key))):
        # Cloud's periodic look decides there; only a seen fall opens a case.
        monitor._emit('candidate_rejected', reason='cleared_place')
        return None
    if not monitor._make_room():
        monitor._emit('candidate_rejected', reason='incident_capacity')
        return None
    current = FallIncident(
        incident_id=str(uuid4()),
        subject_key=candidate.subject_key,
        kind=candidate.kind,
        opened_at=now,
        last_observed_at=candidate.observed_at,
        last_candidate_id=candidate.candidate_id,
        sensors=candidate.sensors,
        candidate_sources=(candidate.source,),
        normal_evidence_after=candidate.observed_at,
        subject_association_token=monitor._subject_evidence.token_at(
            candidate.subject_key, candidate.observed_at,
        ),
    )
    monitor._incidents[current.incident_id] = current
    monitor._emit('incident_opened', current, reason=display_reason)
    monitor._log_pose_place(current)
    monitor._remember_case_place(current)
    monitor._record_candidate_clip(current, candidate)
    if monitor._runtime_cloud_block is not None:
        current.pending = False
        monitor._failure(current, monitor._runtime_cloud_block)
    return current.incident_id
