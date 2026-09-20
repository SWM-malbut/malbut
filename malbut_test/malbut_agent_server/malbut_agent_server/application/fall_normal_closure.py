"""Approved narrow normal-closure rule; no inference or guessed thresholds.

All inputs belong to the same incident. Sensor/pose producers must explicitly
supply a valid subject check; missing detections can never satisfy this rule.
"""

from malbut_agent_server.domain.fall_monitoring import (
    FallIncident, IncidentState, SubjectCheckState, VideoAssessment, VoiceAnswer,
)


def normal_path_ready(incident: FallIncident) -> bool:
    return (
        incident.state not in {IncidentState.HELP_REQUIRED, IncidentState.RESOLVED}
        and incident.candidate_sources == ('yolo_pose',)
        and not incident.auto_normal_blocked
        and not incident.fall_seen
        and incident.notification_level is None
        and incident.answer is VoiceAnswer.OKAY
        and incident.answer_question_played
        and not incident.pending
        and incident.last_failure is None
        and incident.video is not None
        and incident.video.assessment is VideoAssessment.NORMAL_ACTIVITY
        and incident.video_revision == incident.revision
        and bool(incident.normal_checks)
        and all(check.evidence_revision == incident.revision
                for check in incident.normal_checks)
    )


def normal_closure_supported(incident: FallIncident, *, now: float,
                             max_observation_age_s: float) -> bool:
    if not normal_path_ready(incident) or len(incident.normal_checks) != 2:
        return False
    first, last = incident.normal_checks
    observation = incident.subject_observation
    return (
        last.window_end > first.window_end
        and last.request_id != first.request_id
        and observation is not None
        and observation.incident_id == incident.incident_id
        and observation.subject_key == incident.subject_key
        and observation.evidence_revision == incident.revision
        and observation.request_id == last.request_id
        and observation.association_verified
        and observation.state is SubjectCheckState.CLEAR
        and last.window_end >= incident.normal_evidence_after
        and observation.observed_at >= last.window_end
        and 0 <= now - observation.observed_at <= max_observation_age_s
    )
