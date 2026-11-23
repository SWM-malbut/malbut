"""Single-flight Cloud execution loop, guard checks, and retry fallback."""

import asyncio
from dataclasses import replace
from typing import Any, Optional
from uuid import uuid4

from malbut_agent_server.application.cloud_fall.constants import (
    GUARD_FAILURES,
    CloudAnalysisStatus,
)
from malbut_agent_server.domain.fall_monitoring import (
    CloudAnalysisExplanation,
    CloudFallReply,
    CloudFallRequest,
    FallIncident,
    IncidentState,
    NormalVideoCheck,
    VideoAssessment,
)
from malbut_agent_server.ports.cloud_fall import CloudFallProviderError


def cloud_block(monitor: Any, now: float) -> Optional[str]:
    """Return the active reason Cloud requests are blocked, or None."""
    if not monitor._enabled or not monitor._camera:
        return 'monitoring_disabled'
    if not monitor._consent:
        return 'cloud_consent_missing'
    if not monitor._connected:
        return 'cloud_disconnected'
    while monitor._calls and monitor._calls[0] <= now - 60:
        monitor._calls.popleft()
    if len(monitor._calls) >= monitor.policy.max_calls_per_minute:
        return 'cloud_rate_limit'
    return None


def consume_exception(task: asyncio.Task[Any]) -> None:
    """Retrieve exceptions from cancelled/abandoned background tasks cleanly."""
    if not task.cancelled():
        task.exception()


def retry_or_ask(monitor: Any, incident: FallIncident, reason: str) -> None:
    """A Pose case nobody was asked about: send it again, then ask anyway.

    The retries use the existing recheck budget and interval. When every
    attempt failed, the video could not be judged; the person is asked on
    the Pose evidence rather than left without a question.
    """
    if (reason in GUARD_FAILURES or incident.subject_key is None
            or incident.question_id is not None or incident.answer is not None
            or incident.state in (IncidentState.RESOLVED, IncidentState.HELP_REQUIRED)):
        return
    if incident.rechecks < monitor.policy.max_rechecks:
        incident.pending = True  # next_attempt_at already spaces the retry.
        return
    incident.video = CloudFallReply(
        VideoAssessment.UNOBSERVABLE, '영상 분석 실패: 자세 감지 근거로 확인',
    )
    incident.video_revision = incident.revision
    incident.auto_normal_blocked = True
    monitor.ask_question(incident.incident_id)


def handle_failure(
    monitor: Any,
    incident: Optional[FallIncident],
    reason: str,
    *,
    request: Any = None,
) -> None:
    """Record a Cloud analysis failure, update status, and schedule retry/ask."""
    if request is not None:
        state = 'failed'
        error = reason
        if reason in {'cloud_permission_changed', 'worker_cancelled'}:
            state = (
                'cancel_requested'
                if monitor._task is not None and not monitor._task.done()
                else 'canceled'
            )
            error = ''
        monitor._analysis_status = CloudAnalysisStatus(
            state, request.request_id, request.purpose, error,
        )
    if incident:
        if (request is not None
                and (request.evidence_revision != incident.revision
                     or incident.state is IncidentState.RESOLVED
                     or incident.situation_assessment is not None)):
            monitor._emit(
                'stale_analysis_result', incident, reason=reason, request=request,
            )
            return
        incident.last_failure = reason
        # Earlier normal must not stand in for a failed new attempt.
        incident.video = None
        incident.video_revision = None
        incident.normal_checks = ()
        incident.subject_observation = None
        monitor._active_incident = None
        monitor._emit('analysis_unavailable', incident, reason=reason)
        retry_or_ask(monitor, incident, reason)
        monitor._decision_needed(incident)
    else:
        monitor._emit('crosscheck_skipped', reason=reason)


async def run_once(monitor: Any) -> bool:
    """One attempt/scan; queued normal rechecks obey the existing budget."""
    if (monitor._runtime_cloud_block is not None or monitor._running
            or (monitor._task is not None and not monitor._task.done())):
        return False
    monitor._running = True
    try:
        return await run_once_inner(monitor)
    finally:
        monitor._running = False
        monitor._active_incident = None
        if monitor._guardian_closing and not monitor._storage_failed:
            monitor._close_deferred()


async def run_once_inner(monitor: Any) -> bool:
    """Execute the next highest-priority Cloud check, incident analysis, or scan."""
    now = monitor._now()
    if not monitor._enabled or not monitor._camera:
        return False
    looking = next((
        (iid, c) for iid, c in monitor._person_checks.items()
        if c.cloud and not c.asked
    ), None)
    if looking is not None:
        if looking[1].stage == 'posture':
            return await monitor._cloud_posture_check(*looking)
        return await monitor._cloud_person_check(*looking)
    if monitor._comparisons:
        return await monitor._cloud_same_person(*monitor._comparisons.popleft())
    # Local evidence/verification policy does not depend on the next scan
    # or on receiving another successful Cloud response.
    monitor.maintain_associations()
    # First attempts before rechecks, both before periodic background scans.
    ready = [
        i for i in monitor._incidents.values()
        if i.pending and i.state is not IncidentState.RESOLVED
        and now >= i.next_attempt_at
    ]
    ready.sort(key=lambda i: (i.attempts > 0, i.next_attempt_at, i.opened_at))
    incident = ready[0] if ready else None
    if incident is None and now < monitor._scan_anchor + monitor.periodic_interval_s():
        return False
    if incident is not None:
        if incident.attempts > 0 and incident.rechecks >= monitor.policy.max_rechecks:
            incident.pending = False
            monitor._emit('recheck_unavailable', incident, reason='recheck_limit')
            return True
    block = cloud_block(monitor, now)
    window = None
    if block is None:
        try:
            window = monitor.buffer.window(
                end=now,
                duration_s=monitor.policy.clip_window_s,
                max_images=monitor.policy.max_images,
                max_age_s=monitor.policy.max_frame_age_s,
            )
        except ValueError:
            block = 'fresh_rgb_unavailable'
    last_end = incident.last_window_end if incident else monitor._last_scan_end
    if window and window.frames[-1].captured_at <= last_end:
        # Wait for new RGB without consuming the retry budget.
        return False
    if incident is None:
        monitor._scan_anchor = now
    else:
        incident.rechecks += int(incident.attempts > 0)
        incident.attempts += 1
        incident.pending = False
        incident.next_attempt_at = now + monitor.policy.retry_interval_s
        if incident.state is not IncidentState.HELP_REQUIRED:
            incident.state = IncidentState.VERIFYING
    if block or window is None:
        handle_failure(monitor, incident, block or 'fresh_rgb_unavailable')
        return True
    sensors = incident.sensors if incident else None
    if sensors is not None and now - sensors.observed_at > monitor.policy.max_frame_age_s:
        sensors = None
    request = CloudFallRequest(
        request_id=str(uuid4()),
        purpose='incident' if incident else 'crosscheck',
        device_id=monitor.device_id,
        boot_id=monitor.boot_id,
        incident_id=incident.incident_id if incident else None,
        subject_key=incident.subject_key if incident else None,
        evidence_revision=incident.revision if incident else 0,
        window=window,
        sensors=sensors,
    )
    if incident:
        request = replace(request, target=monitor._subject_evidence.target(
            incident.subject_key, window,
        ))
    scene_snapshot = monitor._subject_evidence.snapshot(window) if incident is None else ()
    pose_snapshot = (
        monitor._subject_evidence.association_samples(
            tuple(f.captured_at for f in window.frames),
        )
        if incident is None else ()
    )
    pose_generation = monitor._subject_evidence.generation
    scene_versions = monitor._scene_incident_versions() if incident is None else {}
    if incident:
        incident.last_window_end = window.frames[-1].captured_at
    else:
        monitor._last_scan_end = window.frames[-1].captured_at
    epoch = monitor._epoch
    monitor._active_incident = request.incident_id
    monitor._calls.append(now)
    monitor._task = None
    monitor._analysis_status = CloudAnalysisStatus(
        'waiting_response', request.request_id, request.purpose,
    )
    try:
        monitor._task = asyncio.create_task(monitor._provider.analyze(request))
        monitor._task.add_done_callback(consume_exception)
        done, _ = await asyncio.wait({monitor._task}, timeout=monitor.policy.cloud_timeout_s)
        if monitor._epoch != epoch or monitor._task.cancelled():
            handle_failure(monitor, incident, 'cloud_permission_changed', request=request)
            return True
        if not done:
            monitor._task.cancel()
            handle_failure(monitor, incident, 'cloud_timeout', request=request)
            return True
        reply = monitor._task.result()
        if not isinstance(reply, CloudFallReply):
            raise ValueError('invalid reply contract')
    except asyncio.CancelledError:
        if monitor._task is not None:
            monitor._task.cancel()
        handle_failure(monitor, incident, 'worker_cancelled', request=request)
        raise
    except CloudFallProviderError as error:
        handle_failure(monitor, incident, error.code, request=request)
        return True
    except Exception:
        handle_failure(monitor, incident, 'cloud_failed_or_invalid_response', request=request)
        return True
    finally:
        monitor._active_incident = None
    monitor._analysis_status = CloudAnalysisStatus(
        'completed', request.request_id, request.purpose,
    )
    if incident:
        _apply_incident_reply(monitor, incident, request, reply)
    else:
        monitor._record_crosscheck(
            request, reply, scene_snapshot, scene_versions,
            pose_snapshot=pose_snapshot, pose_generation=pose_generation,
        )
        monitor._emit('crosscheck_completed', request=request, reply=reply)
    return True


def _apply_incident_reply(
    monitor: Any,
    incident: FallIncident,
    request: CloudFallRequest,
    reply: CloudFallReply,
) -> None:
    if (incident.state is IncidentState.RESOLVED
            or incident.situation_assessment is not None):
        monitor._emit(
            'stale_analysis_result', incident,
            reason='confirmation_already_completed', request=request, reply=reply,
        )
        return
    # A late result cannot clear new evidence, but an observed fall
    # in this incident's earlier video must not disappear from history.
    incident.fall_seen |= reply.assessment is VideoAssessment.OBSERVED_FALL
    if reply.assessment is not VideoAssessment.NORMAL_ACTIVITY:
        incident.auto_normal_blocked = True
    # Keep old evidence in events, but never clear a newer change with it.
    if request.evidence_revision != incident.revision:
        monitor._emit(
            'stale_analysis_result', incident, reason=reply.assessment.value,
            request=request, reply=reply,
        )
        return
    incident.video = reply
    incident.video_revision = request.evidence_revision
    incident.last_failure = None
    if reply.assessment is not VideoAssessment.NORMAL_ACTIVITY:
        # A Cloud-suspected/unobservable case is not the approved
        # YOLO-only + initial-normal automatic closure branch.
        incident.normal_checks = ()
        incident.subject_observation = None
    elif request.window.frames[-1].captured_at >= incident.normal_evidence_after:
        check = NormalVideoCheck(
            request.request_id,
            request.evidence_revision,
            request.window.frames[-1].captured_at,
            request.target.association_token if request.target else None,
        )
        checks = incident.normal_checks
        if not checks or check.window_end > checks[-1].window_end:
            incident.normal_checks = (checks + (check,))[-2:]
    monitor._emit(
        'analysis_completed', incident, request=request, reply=reply,
        analysis=CloudAnalysisExplanation.from_reply(request, reply),
    )
    if ((reply.assessment is not VideoAssessment.NORMAL_ACTIVITY or incident.fall_seen)
            and incident.question_id is None):
        monitor.ask_question(incident.incident_id)
    monitor._decision_needed(incident)
