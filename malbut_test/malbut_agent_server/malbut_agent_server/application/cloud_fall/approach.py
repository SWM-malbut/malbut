"""Approach, close-range person check, whole-body posture, and revisit flows."""

import asyncio
from dataclasses import replace
import math
from typing import Any, Optional
from uuid import uuid4

from malbut_agent_server.application.cloud_fall.constants import (
    APPROACH_OUTCOMES,
    COMPARE_WAIT_S,
    FIRST_SCENES_MAX,
    FLOOR_ACTIVE_LOOK,
    FLOOR_ACTIVE_REASON,
    NOT_CONFIRMED_REASONS,
    OFF_FLOOR_LOOKS,
    OFF_FLOOR_REASONS,
    PERSON_POSE_WAIT_S,
    POSTURE_GAP_S,
    POSTURE_SETTLE_S,
    STOP_REASONS,
    VIEW_LIMIT_S,
    WAITING_REASONS,
    PersonCheck,
)
from malbut_agent_server.application.cloud_fall.places import emit_place_log
from malbut_agent_server.application.fall_scene_place import SAME_PLACE_M
from malbut_agent_server.domain.fall_monitoring import (
    CloudAnalysisExplanation,
    FallIncident,
    IncidentState,
    NotificationLevel,
    PersonCheckReply,
    PersonCheckRequest,
    PostureCheckReply,
    PostureCheckRequest,
    SamePersonCheckReply,
    SamePersonCheckRequest,
    VideoAssessment,
    VoiceAnswer,
    identifier,
)


def approach_target(monitor: Any, incident: FallIncident) -> Any:
    """A map point when a person is not certain there; None asks right away."""
    if incident.subject_key is not None:
        latest = monitor._subject_evidence.latest(incident.subject_key)
        if latest is not None and latest[2].strong is True and latest[2].association_usable:
            return None  # A clear Pose person: no need to look closer.
    return spot(monitor, incident)


def spot(monitor: Any, incident: FallIncident) -> Any:
    """The suspicion's map point, or None (approach off, no map point measured)."""
    if not monitor.approach_enabled or monitor._place is None:
        return None
    if incident.subject_key is None:
        return incident.scene_points[-1] if incident.scene_points else None
    latest = monitor._subject_evidence.latest(incident.subject_key)
    if latest is None or latest[2].box is None:
        return None
    try:
        return monitor._place.locate_near(latest[0], latest[2].box)
    except Exception:
        return None  # Auxiliary: never stops the question.


def remember_first_scene(monitor: Any, incident: FallIncident) -> None:
    """Keep up to two frames of the scene that raised the question, with the box."""
    iid = incident.incident_id
    if iid in monitor._first_scenes:
        return
    try:
        window = monitor.buffer.window(
            end=monitor._now(),
            duration_s=2.0,
            max_images=2,
            max_age_s=monitor.policy.max_frame_age_s,
        )
    except ValueError:
        return  # Nothing fresh: a later comparison is simply unclear.
    if incident.subject_key is None:
        box = incident.scene_boxes[-1] if incident.scene_boxes else None
    else:
        latest = monitor._subject_evidence.latest(incident.subject_key)
        box = latest[2].box if latest is not None else None
    monitor._first_scenes[iid] = (window, box)
    while len(monitor._first_scenes) > FIRST_SCENES_MAX:
        monitor._first_scenes.popitem(last=False)


def approach_result(
    monitor: Any,
    *,
    incident_id: str,
    question_id: str,
    evidence_revision: int,
    outcome: str,
) -> bool:
    """The drive ended; on arrival look for a person before anyone is asked."""
    del evidence_revision
    if outcome not in APPROACH_OUTCOMES:
        raise ValueError('invalid approach outcome')
    incident = monitor._incidents.get(incident_id)
    if (incident is None or incident.state is IncidentState.RESOLVED
            or incident.question_id != question_id):
        return False
    target, incident.approach_target = incident.approach_target, None
    monitor._emit('approach_completed', incident, question_id=question_id, reason=outcome)
    if outcome == 'arrived' and target is not None:
        now = monitor._now()
        monitor._person_checks[incident_id] = PersonCheck(
            question_id, target, now, now + PERSON_POSE_WAIT_S,
        )
        advance_person_checks(monitor)
    return True


def return_result(
    monitor: Any,
    *,
    incident_id: str,
    question_id: str,
    evidence_revision: int,
    outcome: str,
) -> bool:
    """Record completion of returning from a spot check."""
    del evidence_revision
    monitor._spot_checks.pop(incident_id, None)
    incident = monitor._incidents.get(incident_id)
    if incident is None:
        return False
    monitor._emit(
        'approach_returned',
        incident,
        question_id=question_id,
        reason='returned' if outcome == 'returned' else 'return_failed',
    )
    return True


def unasked(monitor: Any, incident_id: str, question_id: str) -> Optional[FallIncident]:
    """The open incident still waiting for this question, or None."""
    identifier(incident_id)
    identifier(question_id)
    incident = monitor._incidents.get(incident_id)
    if (incident is None or incident.state is IncidentState.RESOLVED
            or incident.question_id != question_id or incident.answer is not None):
        return None
    return incident


def check_waiting(
    monitor: Any,
    *,
    incident_id: str,
    question_id: str,
    evidence_revision: int,
    reason: str,
) -> bool:
    """The fall rules could not ask yet: 직접 움직이기 or another job (recorded only)."""
    del evidence_revision
    if reason not in WAITING_REASONS:
        raise ValueError('invalid waiting reason')
    incident = unasked(monitor, incident_id, question_id)
    if incident is None:
        return False
    monitor._emit('check_waiting', incident, question_id=question_id, reason=reason)
    return True


def check_not_confirmed(
    monitor: Any,
    *,
    incident_id: str,
    question_id: str,
    evidence_revision: int,
    reason: str,
) -> bool:
    """A minute of waiting: the guardian is alerted (urgent, once); the case stays open."""
    del evidence_revision
    if reason not in NOT_CONFIRMED_REASONS:
        raise ValueError('invalid not-confirmed reason')
    incident = unasked(monitor, incident_id, question_id)
    if incident is None:
        return False
    if incident.state is not IncidentState.HELP_REQUIRED:
        incident.state = IncidentState.RECHECK_REQUIRED
    monitor._emit('check_not_confirmed', incident, question_id=question_id, reason=reason)
    monitor._notify_once(incident, NotificationLevel.URGENT, 'robot_could_not_confirm')
    return True


def check_stopped(
    monitor: Any,
    *,
    incident_id: str,
    question_id: str,
    evidence_revision: int,
    reason: str,
) -> bool:
    """Someone said "그만" during the check: no question, the guardian checks instead."""
    del evidence_revision
    if reason not in STOP_REASONS:
        raise ValueError('invalid stop reason')
    monitor._spot_checks.pop(incident_id, None)
    incident = unasked(monitor, incident_id, question_id)
    if incident is None:
        return False
    # Not an answer: nobody said how they are. FAILED keeps the question from
    # being handed over again, as for a check the robot could not finish.
    incident.answer = VoiceAnswer.FAILED
    if incident.state is not IncidentState.HELP_REQUIRED:
        incident.state = IncidentState.RECHECK_REQUIRED
    monitor._emit('check_stopped', incident, question_id=question_id, reason=reason)
    monitor._notify_once(incident, NotificationLevel.CHECK, 'check_stopped_by_user')
    monitor._questions[incident_id] = replace(incident)
    return True


def revisit_started(
    monitor: Any,
    *,
    incident_id: str,
    question_id: str,
    evidence_revision: int,
    reason: str,
) -> bool:
    """The robot drives back to the spot before asking; that spot is this incident's."""
    del evidence_revision
    if reason not in WAITING_REASONS:
        raise ValueError('invalid revisit reason')
    incident = unasked(monitor, incident_id, question_id)
    if incident is None:
        return False
    monitor._emit('revisit_started', incident, question_id=question_id, reason=reason)
    if incident.revisit_target is not None:
        monitor._spot_checks[incident_id] = (incident.revisit_target, monitor._now())
    return True


def revisit_result(
    monitor: Any,
    *,
    incident_id: str,
    question_id: str,
    evidence_revision: int,
    outcome: str,
) -> bool:
    """Back at the spot: look again as on the first approach, without closing."""
    del evidence_revision
    if outcome not in APPROACH_OUTCOMES:
        raise ValueError('invalid revisit outcome')
    incident = unasked(monitor, incident_id, question_id)
    if incident is None:
        return False
    monitor._emit('approach_completed', incident, question_id=question_id, reason=outcome)
    if outcome == 'arrived' and incident.revisit_target is not None:
        now = monitor._now()
        monitor._person_checks[incident_id] = PersonCheck(
            question_id,
            incident.revisit_target,
            now,
            now + PERSON_POSE_WAIT_S,
            revisit=True,
        )
        advance_person_checks(monitor)
    return True


def compare_later(monitor: Any, iid: str, question_id: str, off_floor: bool) -> None:
    """Compare the person seen now with the first scene; the robot never waits for it.

    After an off-floor look the guardian's notification follows the comparison.
    """
    first = monitor._first_scenes.get(iid)
    after = None
    if first is not None and monitor._cloud_block(monitor._now()) is None:
        try:
            after = monitor.buffer.window(
                end=monitor._now(),
                duration_s=2.0,
                max_images=2,
                max_age_s=monitor.policy.max_frame_age_s,
            )
        except ValueError:
            after = None
    if after is None or not callable(getattr(monitor._provider, 'compare_person', None)):
        finish_comparison(monitor, iid, question_id, off_floor, 'unclear', None)
        return
    monitor._comparisons.append((iid, question_id, off_floor, first, after, monitor._now()))


async def cloud_same_person(
    monitor: Any,
    iid: str,
    question_id: str,
    off_floor: bool,
    first: Any,
    after: Any,
    _queued: float,
) -> bool:
    """Run one asynchronous Cloud clothing/appearance comparison for a revisit."""
    del _queued
    incident = monitor._incidents.get(iid)
    if incident is None or incident.state is IncidentState.RESOLVED:
        return True
    now = monitor._now()
    if monitor._cloud_block(now) is not None:
        finish_comparison(monitor, iid, question_id, off_floor, 'unclear', None)
        return True
    request = SamePersonCheckRequest(str(uuid4()), first[0], first[1], after)
    monitor._calls.append(now)
    epoch, reply = monitor._epoch, None
    try:
        monitor._task = asyncio.create_task(monitor._provider.compare_person(request))
        monitor._task.add_done_callback(monitor._consume_exception)
        done, _ = await asyncio.wait({monitor._task}, timeout=monitor.policy.cloud_timeout_s)
        if not done:
            monitor._task.cancel()
        elif monitor._epoch == epoch and not monitor._task.cancelled():
            reply = monitor._task.result()
    except asyncio.CancelledError:
        if monitor._task is not None:
            monitor._task.cancel()
        raise
    except Exception:
        reply = None  # Blocked, timed out or malformed: unclear.
    verdict, analysis = 'unclear', None
    if isinstance(reply, SamePersonCheckReply):
        verdict = reply.verdict
        try:
            analysis = CloudAnalysisExplanation(
                request.request_id, 'same_person_check', VideoAssessment.UNOBSERVABLE,
                reply.explanation,
            )
        except (ValueError, TypeError, UnicodeError):
            analysis = None
    finish_comparison(monitor, iid, question_id, off_floor, verdict, analysis)
    return True


def finish_comparison(
    monitor: Any,
    iid: str,
    question_id: str,
    off_floor: bool,
    verdict: str,
    analysis: Any,
) -> None:
    """Emit the revisit comparison result and guardian notification when off-floor."""
    incident = monitor._incidents.get(iid)
    if incident is None or incident.state is IncidentState.RESOLVED:
        return
    monitor._emit(
        'same_person_compared',
        incident,
        question_id=question_id,
        reason=verdict,
        analysis=analysis,
    )
    if not off_floor:
        return  # Asked as before: the answer decides the notification.
    if verdict == 'same':
        monitor._notify_once(incident, NotificationLevel.INFO, 'revisit_same_person_off_floor')
    else:
        monitor._notify_once(
            incident, NotificationLevel.CHECK, 'revisit_person_unclear_off_floor',
        )


def expire_comparisons(monitor: Any) -> None:
    """Resolve queued same-person comparisons as unclear once their window expires."""
    now = monitor._now()
    while monitor._comparisons and now - monitor._comparisons[0][5] > COMPARE_WAIT_S:
        iid, question_id, off_floor, *_ = monitor._comparisons.popleft()
        finish_comparison(monitor, iid, question_id, off_floor, 'unclear', None)


def pose_sees_person(monitor: Any, check: PersonCheck) -> bool:
    """Return True when Pose detects a strong person at the checked target spot."""
    for stamp, poses in monitor._subject_evidence.frames_since(check.started):
        for pose in poses:
            if pose.strong is not True or not pose.association_usable or pose.box is None:
                continue
            point = None
            if monitor._place is not None:
                try:
                    point = monitor._place.locate_near(stamp, pose.box)
                except Exception:
                    point = None
            if point is not None:
                if math.dist(point, check.target) <= SAME_PLACE_M:
                    return True
            elif 0.25 <= (pose.box[0] + pose.box[2]) / 2 <= 0.75:
                return True  # The robot faces the spot: a person in the middle.
    return False


def advance_person_checks(monitor: Any) -> None:
    """Advance close-range person/view/posture state machines against local clock."""
    now = monitor._now()
    for iid, check in tuple(monitor._person_checks.items()):
        incident = monitor._incidents.get(iid)
        if (incident is None or incident.state is IncidentState.RESOLVED
                or incident.question_id != check.question_id):
            del monitor._person_checks[iid]
        elif check.cloud:
            continue
        elif check.stage == 'view':
            if now >= check.deadline:
                finish_posture_check(monitor, iid, 'body_unseen')  # Lost move report.
        elif check.stage == 'posture':
            if now >= check.deadline:
                check.cloud = True  # run_once takes the next Cloud look.
        elif pose_sees_person(monitor, check):
            if monitor.full_view_enabled:
                start_view(monitor, iid, check, now)
            else:
                finish_person_check(monitor, iid, 'person')
        elif now >= check.pose_deadline:
            check.cloud = True  # run_once asks Cloud once.


def start_view(
    monitor: Any,
    iid: str,
    check: PersonCheck,
    now: float,
    analysis: Any = None,
) -> None:
    """A person, seen only in part from 1 m: the coordinator moves to see all of it."""
    incident = monitor._incidents[iid]
    check.stage, check.cloud, check.asked = 'view', False, False
    check.deadline = now + VIEW_LIMIT_S + 30.0  # Beyond the coordinator's own limit.
    monitor._emit(
        'person_check_completed',
        incident,
        question_id=check.question_id,
        reason='partial_body',
        analysis=analysis,
    )


def start_posture_check(monitor: Any, iid: str, check: PersonCheck, now: float) -> None:
    """Let the camera settle after the move, then the first Cloud look."""
    del monitor, iid
    check.stage, check.cloud, check.asked, check.looks = 'posture', False, False, ()
    check.started, check.deadline = now, now + POSTURE_SETTLE_S


def view_result(
    monitor: Any,
    *,
    incident_id: str,
    question_id: str,
    evidence_revision: int,
    outcome: str,
) -> bool:
    """The move to the whole-body spot ended; on arrival read the posture."""
    del evidence_revision
    if outcome not in APPROACH_OUTCOMES:
        raise ValueError('invalid view outcome')
    incident = monitor._incidents.get(incident_id)
    check = monitor._person_checks.get(incident_id)
    if (incident is None or incident.state is IncidentState.RESOLVED
            or incident.question_id != question_id or check is None or check.stage != 'view'):
        return False
    monitor._emit('view_completed', incident, question_id=question_id, reason=outcome)
    if outcome == 'arrived':
        start_posture_check(monitor, incident_id, check, monitor._now())
    else:
        finish_posture_check(monitor, incident_id, 'body_unseen')
    return True


async def cloud_posture_check(monitor: Any, iid: str, check: PersonCheck) -> bool:
    """Ask Cloud where the full body rests across two spaced looks."""
    check.asked = True
    now = monitor._now()
    block = monitor._cloud_block(now)
    window = None
    if block is None:
        try:
            window = monitor.buffer.window(
                end=now,
                duration_s=2.0,
                max_images=3,
                max_age_s=monitor.policy.max_frame_age_s,
            )
        except ValueError:
            block = 'fresh_rgb_unavailable'
    if block is not None or window is None:
        finish_posture_check(monitor, iid, 'posture_unconfirmed')  # Cannot agree: ask.
        return True
    request = PostureCheckRequest(str(uuid4()), window)
    monitor._calls.append(now)
    epoch, reply = monitor._epoch, None
    try:
        monitor._task = asyncio.create_task(monitor._provider.check_posture(request))
        monitor._task.add_done_callback(monitor._consume_exception)
        done, _ = await asyncio.wait({monitor._task}, timeout=monitor.policy.cloud_timeout_s)
        if not done:
            monitor._task.cancel()
        elif monitor._epoch == epoch and not monitor._task.cancelled():
            reply = monitor._task.result()
    except asyncio.CancelledError:
        if monitor._task is not None:
            monitor._task.cancel()
        raise
    except Exception:
        reply = None  # Unclear or failed: ask rather than close.
    verdict = reply.verdict if isinstance(reply, PostureCheckReply) else None
    incident = monitor._incidents.get(iid)
    calm = verdict in OFF_FLOOR_LOOKS or (
        verdict == FLOOR_ACTIVE_LOOK and not check.revisit
        and incident is not None and not incident.fall_seen
    )
    analysis = None
    if verdict is not None and isinstance(reply, PostureCheckReply):
        seen = (
            VideoAssessment.NORMAL_ACTIVITY if calm
            else VideoAssessment.SUSPECTED_FALL
        )
        try:
            analysis = CloudAnalysisExplanation(
                request.request_id, 'posture_check', seen, reply.explanation,
            )
        except (ValueError, TypeError, UnicodeError):
            analysis = None
    check.looks += (verdict or 'failed',)
    if calm and len(check.looks) < 2:
        # One look is not enough to skip the question: look again on new frames.
        check.cloud, check.asked = False, False
        check.deadline = monitor._now() + POSTURE_GAP_S
        return True
    if calm and all(v in OFF_FLOOR_LOOKS for v in check.looks):
        reason = (
            'standing' if all(v == 'standing' for v in check.looks)
            else 'sitting_off_floor'
        )
    elif calm and all(v == FLOOR_ACTIVE_LOOK for v in check.looks):
        reason = FLOOR_ACTIVE_REASON
    elif calm:
        reason = 'posture_unconfirmed'  # Off the floor once, on it once: ask.
    else:
        reason = (
            'on_floor' if verdict in ('floor', FLOOR_ACTIVE_LOOK)
            else 'posture_unconfirmed'
        )
    finish_posture_check(monitor, iid, reason, analysis)
    return True


def finish_posture_check(
    monitor: Any,
    iid: str,
    reason: str,
    analysis: Any = None,
) -> None:
    """Apply the outcome of a whole-body posture check."""
    check = monitor._person_checks.pop(iid, None)
    incident = monitor._incidents.get(iid)
    if check is None or incident is None or incident.state is IncidentState.RESOLVED:
        return
    if check.revisit:
        # Off the floor: the rules drive back without asking and the guardian is
        # told after the comparison. Anything else is asked as before.
        monitor._emit(
            'posture_check_completed',
            incident,
            question_id=check.question_id,
            reason=reason,
            analysis=analysis,
        )
        compare_later(monitor, iid, check.question_id, reason in OFF_FLOOR_REASONS)
        return
    if reason in OFF_FLOOR_REASONS or reason == FLOOR_ACTIVE_REASON:
        if monitor._active_incident == iid:
            reason = 'posture_unconfirmed'  # Its analysis is in flight: ask instead.
        else:
            incident.pending = False
    emit_place_log(
        monitor,
        f'fall posture: case {iid[:8]} Cloud looks '
        f'{", ".join(check.looks) or "none"}: {reason}',
    )
    monitor._emit(
        'posture_check_completed',
        incident,
        question_id=check.question_id,
        reason=reason,
        analysis=analysis,
    )
    if reason in OFF_FLOOR_REASONS:
        monitor.resolve(iid, revision=incident.revision, reason='off_floor_verified')
    elif reason == FLOOR_ACTIVE_REASON:
        monitor.resolve(iid, revision=incident.revision, reason='floor_activity_verified')


async def cloud_person_check(monitor: Any, iid: str, check: PersonCheck) -> bool:
    """Ask Cloud once from close range whether a person is actually present."""
    check.asked = True
    now = monitor._now()
    block = monitor._cloud_block(now)
    window = None
    if block is None:
        try:
            window = monitor.buffer.window(
                end=now,
                duration_s=2.0,
                max_images=3,
                max_age_s=monitor.policy.max_frame_age_s,
            )
        except ValueError:
            block = 'fresh_rgb_unavailable'
    if block is not None or window is None:
        finish_person_check(monitor, iid, 'person')  # Cannot look: ask.
        return True
    request = PersonCheckRequest(str(uuid4()), window)
    monitor._calls.append(now)
    epoch, reply = monitor._epoch, None
    try:
        monitor._task = asyncio.create_task(monitor._provider.check_person(request))
        monitor._task.add_done_callback(monitor._consume_exception)
        done, _ = await asyncio.wait({monitor._task}, timeout=monitor.policy.cloud_timeout_s)
        if not done:
            monitor._task.cancel()
        elif monitor._epoch == epoch and not monitor._task.cancelled():
            reply = monitor._task.result()
    except asyncio.CancelledError:
        if monitor._task is not None:
            monitor._task.cancel()
        raise
    except Exception:
        reply = None  # Unclear or failed: ask rather than close.
    not_person = isinstance(reply, PersonCheckReply) and reply.verdict == 'not_person'
    analysis = None
    if isinstance(reply, PersonCheckReply):
        seen = (
            VideoAssessment.NORMAL_ACTIVITY if not_person
            else VideoAssessment.SUSPECTED_FALL
        )
        try:
            analysis = CloudAnalysisExplanation(
                request.request_id, 'person_check', seen, reply.explanation,
            )
        except (ValueError, TypeError, UnicodeError):
            analysis = None
    if (monitor.full_view_enabled and isinstance(reply, PersonCheckReply)
            and reply.verdict == 'person' and iid in monitor._person_checks):
        # A person, but Pose did not see the whole body in time.
        start_view(monitor, iid, check, monitor._now(), analysis)
        return True
    finish_person_check(monitor, iid, 'not_a_person' if not_person else 'person', analysis)
    return True


def finish_person_check(
    monitor: Any,
    iid: str,
    reason: str,
    analysis: Any = None,
) -> None:
    """Apply the outcome of a close-range person check."""
    check = monitor._person_checks.pop(iid, None)
    incident = monitor._incidents.get(iid)
    if check is None or incident is None or incident.state is IncidentState.RESOLVED:
        return
    if check.revisit:
        if reason == 'not_a_person':
            # Nobody there is no proof of anything: they may have crawled away or
            # fallen elsewhere. Kept open; the guardian checks (목업 28번).
            monitor._emit(
                'person_check_completed',
                incident,
                question_id=check.question_id,
                reason='nobody_there',
                analysis=analysis,
            )
            monitor._notify_once(incident, NotificationLevel.CHECK, 'revisit_nobody_there')
            return
        monitor._emit(
            'person_check_completed',
            incident,
            question_id=check.question_id,
            reason=reason,
            analysis=analysis,
        )
        compare_later(monitor, iid, check.question_id, False)  # Asked; compared meanwhile.
        return
    if reason == 'not_a_person':
        if monitor._active_incident == iid:
            reason = 'person'  # Its analysis is in flight: ask instead of closing.
        else:
            incident.pending = False
    monitor._emit(
        'person_check_completed',
        incident,
        question_id=check.question_id,
        reason=reason,
        analysis=analysis,
    )
    if reason == 'not_a_person':
        monitor.resolve(iid, revision=incident.revision, reason='not_a_person')
