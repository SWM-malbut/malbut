"""Confirmation handoff, agent replies, notifications, and incident closure."""

from collections import deque
from dataclasses import replace
from typing import Any, Optional, Tuple
from uuid import uuid4

from malbut_agent_server.application.cloud_fall.constants import CHECK_FAILURE_REASONS
from malbut_agent_server.application.fall_normal_closure import (
    normal_closure_supported,
    normal_path_ready,
)
from malbut_agent_server.domain.fall_monitoring import (
    AgentCheckReply,
    FallIncident,
    FallRuntimeEvent,
    IncidentState,
    NotificationLevel,
    VideoAssessment,
    VoiceAnswer,
    identifier,
)


def pending_question_id(monitor: Any, incident: FallIncident) -> Optional[str]:
    """Return the active unanswered question ID for an incident, if any."""
    question = monitor._questions.get(incident.incident_id)
    return question.question_id if question is not None and question.answer is None else None


def ask_question(monitor: Any, incident_id: str) -> Optional[str]:
    """Initiate a confirmation question and optional approach target for an incident."""
    incident = monitor._incidents[incident_id]
    if incident.state is IncidentState.RESOLVED:
        return None
    if incident.question_id is not None and incident.answer is None:
        return incident.question_id
    incident.question_id = str(uuid4())
    incident.answer = None
    incident.answer_question_played = False
    incident.situation_assessment = None
    incident.help_needed = None
    incident.approach_target = monitor._approach_target(incident)
    incident.revisit_target = (
        incident.approach_target
        if incident.approach_target is not None
        else monitor._spot(incident)
    )
    if incident.approach_target is not None:
        monitor._spot_checks[incident_id] = (incident.approach_target, monitor._now())
    monitor._remember_first_scene(incident)
    if incident.video is not None:
        monitor._questions[incident_id] = replace(incident)
    monitor._emit(
        'question_requested',
        incident,
        question_id=incident.question_id,
        reply=incident.video,
        reason=(
            'prior_fall_observed'
            if incident.fall_seen and incident.video
            and incident.video.assessment is VideoAssessment.NORMAL_ACTIVITY
            else None
        ),
        approach_target=incident.approach_target,
        revisit_target=incident.revisit_target,
    )
    if incident.approach_target is not None:
        running = monitor._running_missions
        monitor._emit(
            'approach_started',
            incident,
            question_id=incident.question_id,
            reason=(
                'patrol_stopped' if 'patrol' in running
                else 'follow_stopped' if 'follow_person' in running else None
            ),
        )
    return incident.question_id


def continue_confirmation(monitor: Any, incident: FallIncident) -> None:
    """Reassess the latest evidence once an older question has finished."""
    if incident.state is IncidentState.HELP_REQUIRED:
        return
    incident.question_id = None
    if (incident.video is None or incident.video_revision != incident.revision
            or incident.pending or monitor._active_incident == incident.incident_id):
        return
    if (incident.video.assessment is not VideoAssessment.NORMAL_ACTIVITY
            or incident.fall_seen):
        monitor.ask_question(incident.incident_id)
    else:
        monitor._emit('analysis_completed', incident, reply=incident.video)


def confirmation_result(
    monitor: Any,
    *,
    incident_id: str,
    question_id: str,
    subject_key: Optional[str],
    evidence_revision: int,
    situation_assessment: str,
    help_needed: bool,
) -> bool:
    """Apply the Manager's final result, preserving the user's own account.

    No transport failure is converted into a user's silence here. The
    conversation Agent alone applies its ten-second response deadline.
    """
    for value in (incident_id, question_id):
        identifier(value)
    # Asked instead of driving back: the check of the spot is over.
    monitor._spot_checks.pop(incident_id, None)
    if subject_key is not None:
        identifier(subject_key)
    if (type(evidence_revision) is not int or evidence_revision < 1
            or not isinstance(situation_assessment, str)
            or situation_assessment not in {'confirmed_incident', 'resolved', 'unknown'}
            or type(help_needed) is not bool):
        raise ValueError('invalid confirmation result')
    current = monitor._incidents.get(incident_id)
    question = monitor._questions.get(incident_id, current)
    incident = (
        question if question is not None and current is not None
        and question.revision != current.revision else current
    )
    if (incident is None or current is None
            or incident.question_id != question_id
            or incident.subject_key != subject_key
            or incident.revision != evidence_revision
            or incident.answer is VoiceAnswer.FAILED
            or (incident is not current and current.state is IncidentState.RESOLVED)):
        return False
    if incident.situation_assessment is not None:
        return (
            incident.situation_assessment == situation_assessment
            and incident.help_needed == help_needed
        )
    if incident.state is IncidentState.RESOLVED:
        return False
    incident.situation_assessment = situation_assessment
    incident.help_needed = help_needed
    incident.pending = False
    if incident is not current:
        # This answer belongs to the question's original evidence, not to
        # observations collected while the user was answering it.
        incident.answer = (
            VoiceAnswer.HELP if help_needed else
            VoiceAnswer.UNCLEAR if incident.subject_key is None
            else VoiceAnswer.OKAY
        )
        incident.state = (
            IncidentState.HELP_REQUIRED if help_needed
            else IncidentState.RECHECK_REQUIRED
        )
        monitor._emit(
            'confirmation_completed' if help_needed else 'voice_result',
            incident,
            question_id=question_id,
            reason=(
                'scene_answer_unassociated' if incident.answer is VoiceAnswer.UNCLEAR
                else situation_assessment
            ),
        )
        if incident.answer is VoiceAnswer.UNCLEAR:
            incident.notification_level = current.notification_level
            monitor._notify_once(incident, NotificationLevel.CHECK, 'scene_answer_unconfirmed')
            current.notification_level = incident.notification_level
        if help_needed:
            current.state = IncidentState.HELP_REQUIRED
            current.answer = VoiceAnswer.HELP
            current.help_needed = True
            incident.notification_level = current.notification_level
            monitor._notify(incident, NotificationLevel.URGENT, 'confirmation_help_required')
            current.notification_level = incident.notification_level
        monitor._emit('incident_updated', current, reason='confirmation_finished_newer_evidence')
        continue_confirmation(monitor, current)
        return True
    if incident.subject_key is None and not help_needed:
        # The speaker has not been associated with the person(s) seen by
        # Cloud. Keep the reply, but never treat it as their clearance.
        incident.answer = VoiceAnswer.UNCLEAR
        if incident.state is not IncidentState.HELP_REQUIRED:
            incident.state = IncidentState.RECHECK_REQUIRED
        monitor._emit(
            'voice_result', incident, question_id=question_id,
            reason='scene_answer_unassociated',
        )
        # Someone said they were fine, maybe not the person seen down: tell
        # the guardian (2026-10-12 16:31 sent nothing while someone lay by a bed).
        monitor._notify_once(incident, NotificationLevel.CHECK, 'scene_answer_unconfirmed')
        monitor._questions[incident_id] = replace(incident)
        monitor._emit('decision_required', incident, reason='target_unidentified')
        return True
    incident.answer = VoiceAnswer.HELP if help_needed else VoiceAnswer.OKAY
    incident.state = IncidentState.HELP_REQUIRED if help_needed else IncidentState.RESOLVED
    if not help_needed:
        incident.close_reason = (
            'risk_cleared' if situation_assessment == 'resolved'
            else 'response_completed'
        )
    monitor._questions[incident_id] = replace(incident)
    monitor._emit(
        'confirmation_completed', incident, question_id=question_id,
        reason=situation_assessment,
    )
    if help_needed:
        monitor._notify(incident, NotificationLevel.URGENT, 'confirmation_help_required')
    else:
        # This is an explicit user-informed decision, not automatic video
        # clearance. A later video result may not overturn it.
        monitor._emit('incident_resolved', incident, reason=incident.close_reason)
        monitor._remember_cleared_place(incident)
        monitor._remember_settled_subject(incident)
    return True


def confirmation_failed(
    monitor: Any,
    *,
    incident_id: str,
    question_id: str,
    evidence_revision: int,
    reason: str = 'question_error',
) -> bool:
    """Record an unavailable/failed conversation without inventing an answer.

    The robot could not check by itself (speech off, a failed question, or ten
    minutes of waiting): the guardian is alerted (urgent, once).
    """
    if reason not in CHECK_FAILURE_REASONS:
        raise ValueError('invalid confirmation failure reason')
    monitor._spot_checks.pop(incident_id, None)
    current = monitor._incidents.get(incident_id)
    question = monitor._questions.get(incident_id, current)
    incident = (
        question if question is not None and current is not None
        and question.revision != current.revision else current
    )
    if (incident is None or current is None
            or incident.question_id != question_id
            or incident.revision != evidence_revision
            or incident.state is IncidentState.RESOLVED
            or current.state is IncidentState.RESOLVED
            or incident.situation_assessment is not None):
        return False
    if incident.answer is VoiceAnswer.FAILED:
        return True
    incident.answer = VoiceAnswer.FAILED
    incident.state = IncidentState.RECHECK_REQUIRED
    monitor._emit('agent_check_failed', incident, question_id=question_id, reason=reason)
    monitor._notify_once(incident, NotificationLevel.URGENT, 'robot_could_not_confirm')
    if incident is not current:
        monitor._raise_level(current, NotificationLevel.URGENT)
    monitor._questions[incident_id] = replace(incident)
    if incident is not current:
        monitor._emit('incident_updated', current, reason='confirmation_finished_newer_evidence')
        continue_confirmation(monitor, current)
    return True


def pending_questions(monitor: Any) -> Tuple[FallRuntimeEvent, ...]:
    """Retransmit unfinished handoffs so a late-starting Manager can receive them."""
    events = []
    for current in monitor._incidents.values():
        if current.merged_into_incident_ids:
            # Retransmit the terminal tombstone too: a lost merge event
            # must not leave an old scene question alive in the Manager.
            events.append(FallRuntimeEvent(
                event_id=current.incident_id,
                kind='incident_merged',
                incident_id=current.incident_id,
                question_id=current.question_id,
                evidence_revision=current.revision,
                confirmation_scope='scene',
                reason='findings_associated',
                merged_into_incident_ids=current.merged_into_incident_ids,
            ))
            continue
        if current.state is IncidentState.RESOLVED:
            continue
        incident = monitor._questions.get(current.incident_id, current)
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
            incident_id=incident.incident_id,
            question_id=incident.question_id,
            subject_key=incident.subject_key,
            evidence_revision=incident.revision,
            confirmation_scope='scene' if incident.subject_key is None else 'subject',
            reply=incident.video,
            reason='prior_fall_observed' if normal and incident.fall_seen else None,
            approach_target=current.approach_target,
            revisit_target=current.revisit_target,
        ))
    return tuple(events)


def agent_reply(monitor: Any, reply: AgentCheckReply) -> bool:
    """Agent owns playback, waiting, speaker association and answer interpretation."""
    if not isinstance(reply, AgentCheckReply):
        raise ValueError('invalid Agent reply')
    current = monitor._incidents.get(reply.incident_id)
    question = monitor._questions.get(reply.incident_id, current)
    incident = (
        question if question is not None and current is not None
        and question.revision != current.revision else current
    )
    if (incident is None or current is None
            or incident.question_id != reply.question_id
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
        monitor._notify(incident, NotificationLevel.URGENT, 'help_requested')
    else:
        if answer is VoiceAnswer.FAILED:
            monitor._emit('agent_check_failed', incident, reason='agent_failed')
    monitor._emit('voice_result', incident, reason=answer.value)
    if incident.incident_id in monitor._questions:
        monitor._questions[incident.incident_id] = replace(incident)
    if incident is not current:
        current.notification_level = incident.notification_level
        if answer is VoiceAnswer.HELP:
            current.state = IncidentState.HELP_REQUIRED
            current.answer = VoiceAnswer.HELP
            current.help_needed = True
        monitor._emit('incident_updated', current, reason='confirmation_finished_newer_evidence')
        continue_confirmation(monitor, current)
        return True
    if answer is not VoiceAnswer.HELP:
        decision_needed(monitor, incident)
    return True


def decision_needed(monitor: Any, incident: FallIncident) -> None:
    """Evaluate normal closure or request an explicit decision/recheck."""
    monitor._refresh_subject(incident)
    if (incident.state is IncidentState.RESOLVED
            or incident.answer is None or incident.pending
            or monitor._active_incident == incident.incident_id):
        return
    if incident.video is None and incident.last_failure is None:
        return
    if incident.state not in {IncidentState.HELP_REQUIRED, IncidentState.RESOLVED}:
        incident.state = IncidentState.RECHECK_REQUIRED
    if incident.fall_seen and incident.answer is VoiceAnswer.OKAY:
        monitor._notify(incident, NotificationLevel.INFO, 'fall_observed_person_okay')
    if monitor._enabled and monitor._camera and normal_path_ready(incident):
        if normal_closure_supported(
                incident, now=monitor._now(),
                max_observation_age_s=monitor.policy.max_person_observation_age_s):
            monitor.resolve(
                incident.incident_id,
                revision=incident.revision,
                reason='normal_verified',
            )
            return
        if len(incident.normal_checks) == 1 and monitor.request_recheck(incident.incident_id):
            monitor._emit('decision_required', incident, reason='new_video_recheck_required')
            return
    monitor._emit('decision_required', incident)


def request_recheck(monitor: Any, incident_id: str) -> bool:
    """Queue another Cloud verification attempt within budget."""
    incident = monitor._incidents[incident_id]
    if incident.state is IncidentState.RESOLVED:
        return False
    if incident.subject_key is None:
        # No target exists for an incident-specific request. Periodic
        # scene scans continue; do not turn a bystander's normal result
        # into a recheck of an unidentified person.
        monitor._emit('recheck_unavailable', incident, reason='target_unidentified')
        return False
    if monitor._runtime_cloud_block is not None:
        monitor._emit('recheck_unavailable', incident, reason=monitor._runtime_cloud_block)
        return False
    if incident.attempts > 0 and incident.rechecks >= monitor.policy.max_rechecks:
        monitor._emit('recheck_unavailable', incident, reason='recheck_limit')
        return False
    incident.pending = True
    return True


def mark_unresolved(
    monitor: Any,
    incident_id: str,
    *,
    revision: int,
    suspicion_persists: bool,
) -> None:
    """Fusion caller supplies persistence, not an inferred medical fact."""
    incident = monitor._incidents[incident_id]
    if type(suspicion_persists) is not bool or revision != incident.revision:
        raise ValueError('invalid or stale decision')
    if incident.state is IncidentState.RESOLVED:
        return
    if (incident.pending or monitor._active_incident == incident_id
            or incident.answer is None):
        raise ValueError('verification still pending')
    if incident.state is not IncidentState.HELP_REQUIRED:
        incident.state = IncidentState.RECHECK_REQUIRED
    if (incident.attempts > 0 and incident.rechecks >= monitor.policy.max_rechecks
            and suspicion_persists):
        monitor._notify(
            incident, NotificationLevel.CHECK, 'check_required_not_confirmed_fall',
        )


def resolve(monitor: Any, incident_id: str, *, revision: int, reason: str) -> None:
    """Only a verified fusion/operator decision may call this boundary."""
    incident = monitor._incidents[incident_id]
    if reason not in {
        'normal_verified', 'risk_cleared', 'response_completed',
        'not_a_person', 'off_floor_verified', 'floor_activity_verified',
    }:
        raise ValueError('verified closure reason required')
    if (revision != incident.revision or incident.pending
            or monitor._active_incident == incident_id):
        raise ValueError('stale decision or unfinished analysis')
    if incident.state is IncidentState.RESOLVED:
        return
    if reason == 'normal_verified':
        if not monitor._enabled or not monitor._camera or not normal_closure_supported(
                incident, now=monitor._now(),
                max_observation_age_s=monitor.policy.max_person_observation_age_s):
            raise ValueError('normal closure not supported')
    incident.state = IncidentState.RESOLVED
    incident.close_reason = reason
    monitor._emit('incident_resolved', incident, reason=reason)
    monitor._remember_settled_subject(incident)
    monitor._remember_cleared_place(incident)


def close_by_guardian(monitor: Any, incident_id: str) -> bool:
    """A member pressed 처리 완료 in the app: the robot stops checking this incident."""
    identifier(incident_id)
    incident = monitor._incidents.get(incident_id)
    if incident is None or incident.state is IncidentState.RESOLVED:
        return False  # Unknown here (another boot) or already closed: the list repeats.
    if monitor._active_incident == incident_id:
        # Its analysis is in flight; a result for a closed case is not recordable.
        monitor._guardian_closing.add(incident_id)
        return True
    monitor._guardian_closing.discard(incident_id)
    monitor._person_checks.pop(incident_id, None)
    monitor._spot_checks.pop(incident_id, None)
    monitor._first_scenes.pop(incident_id, None)
    monitor._comparisons = deque(c for c in monitor._comparisons if c[0] != incident_id)
    incident.pending = False
    incident.state = IncidentState.RESOLVED
    incident.close_reason = 'guardian_closed'
    monitor._emit('incident_resolved', incident, reason='guardian_closed')
    return True


def close_deferred(monitor: Any) -> None:
    """Apply deferred guardian closures once in-flight analysis finishes."""
    for incident_id in tuple(monitor._guardian_closing):
        monitor._guardian_closing.discard(incident_id)
        monitor.close_by_guardian(incident_id)
