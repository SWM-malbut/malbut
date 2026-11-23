"""Cloud look execution (filter, arrival, departure, posture) and periodic scans."""

import asyncio
from typing import Any, Coroutine, Optional, Sequence, Tuple
from uuid import uuid4

from malbut_agent_server.application.fall_sensor.constants import (
    ARRIVAL_FRAMES,
    ARRIVAL_WINDOW_S,
    DEPARTURE_FRAMES,
    FURNITURE_HEIGHT_M,
    GUARD_REASONS,
    POSTURE_FRAMES,
    POSTURE_WINDOW_S,
    SHOWN,
    SUPPORT_BAND,
    AnalysisStatus,
    Look,
    Photo,
)
from malbut_agent_server.domain.fall_monitoring import (
    ArrivalCheckRequest,
    CloudAnalysisExplanation,
    CloudFallReply,
    CloudFallRequest,
    DepartureCheckRequest,
    FrameWindow,
    PersonCheckRequest,
    PostureCheckRequest,
    VideoAssessment,
)
from malbut_agent_server.ports.cloud_fall import CloudFallProviderError


async def call_provider(
    sensor: Any,
    request_id: str,
    purpose: str,
    coroutine: Coroutine[Any, Any, Any],
) -> Tuple[Any, Optional[str]]:
    """One Cloud call within the timeout; None on any failure (the reason recorded)."""
    epoch = sensor._epoch
    sensor._calls.append(sensor._now())
    sensor._analysis = AnalysisStatus('waiting_response', request_id, purpose)
    sensor._task = asyncio.ensure_future(coroutine)
    sensor._task.add_done_callback(lambda task: task.cancelled() or task.exception())
    try:
        done, _ = await asyncio.wait({sensor._task}, timeout=sensor.policy.cloud_timeout_s)
        if sensor._epoch != epoch or sensor._task.cancelled():
            sensor._analysis = AnalysisStatus('canceled', request_id, purpose)
            return None, 'cloud_permission_changed'
        if not done:
            sensor._task.cancel()
            sensor._analysis = AnalysisStatus('failed', request_id, purpose, 'cloud_timeout')
            return None, 'cloud_timeout'
        reply = sensor._task.result()
    except asyncio.CancelledError:
        if sensor._task is not None:
            sensor._task.cancel()
        raise
    except CloudFallProviderError as error:
        sensor._analysis = AnalysisStatus('failed', request_id, purpose, error.code)
        return None, error.code
    except Exception:
        sensor._analysis = AnalysisStatus(
            'failed', request_id, purpose, 'cloud_failed_or_invalid_response',
        )
        return None, 'cloud_failed_or_invalid_response'
    sensor._analysis = AnalysisStatus('completed', request_id, purpose)
    return reply, None


def explain_look(
    sensor: Any,
    look_id: str,
    request_id: str,
    purpose: str,
    assessment: VideoAssessment,
    explanation: str,
) -> None:
    """Cache display-only Cloud analysis explanation for one look."""
    try:
        sensor._analyses[look_id] = CloudAnalysisExplanation(
            request_id, purpose, assessment, explanation,
        )
    except (ValueError, TypeError, UnicodeError):
        return  # Missing display evidence never stops the check.
    while len(sensor._analyses) > 128:
        sensor._analyses.popitem(last=False)


def photo_window(
    sensor: Any,
    photo_id: Optional[str],
) -> Tuple[Optional[FrameWindow], Optional[Tuple[float, float, float, float]]]:
    """Return (FrameWindow, box) for a retained sighting photo, if present."""
    photo = sensor._photos.get(photo_id) if photo_id else None
    if photo is None:
        return None, None
    frames = photo.frames
    return FrameWindow(frames, frames[0].captured_at, frames[-1].captured_at, False), photo.box


def now_window(
    sensor: Any,
    now: float,
    duration_s: float,
    count: int,
) -> Optional[FrameWindow]:
    """Build a FrameWindow from the freshest RGB frames ending at now."""
    frames = sensor._frames(now, duration_s, count)
    if not frames:
        return None
    return FrameWindow(frames, frames[0].captured_at, frames[-1].captured_at, False)


def answer_failed(sensor: Any, look: Look, error: Optional[str]) -> None:
    """Report a look failure as guard or failed depending on the error code."""
    if error in GUARD_REASONS:
        sensor._answer(look, 'guard', reason=error)
    else:
        sensor._answer(look, 'failed', reason=error)


async def run_look(sensor: Any, look: Look) -> None:
    """Execute one queued look against Cloud if allowed and within rate budget."""
    if not sensor._cloud_allowed():
        sensor._answer(look, 'guard', reason=sensor._guard_reason())
        return
    blocked = sensor._provider_blocked()
    if blocked is not None:
        # Locally blocked provider (auth/billing/quota cooldown) makes zero HTTP calls;
        # answer immediately instead of starving max_calls_per_minute.
        purpose = {
            'filter': 'incident',
            'arrival': 'arrival_check',
            'departure': 'departure_check',
            'posture': 'posture_check',
        }.get(look.what, look.what)
        sensor._analysis = AnalysisStatus('failed', str(uuid4()), purpose, blocked)
        answer_failed(sensor, look, blocked)
        return
    now = sensor._now()
    if sensor._rate_limited(now):
        sensor._looks.appendleft(look)  # Within the minute's budget: wait, not fail.
        return
    request_id = str(uuid4())
    if look.what == 'filter':
        await run_filter(sensor, look, request_id, now)
    elif look.what == 'arrival':
        await run_arrival(sensor, look, request_id, now)
    elif look.what == 'departure':
        await run_departure(sensor, look, request_id, now)
    else:
        await run_posture(sensor, look, request_id, now)


async def run_filter(sensor: Any, look: Look, request_id: str, now: float) -> None:
    """The first look at a suspicion: the Cloud check that used to follow the opening."""
    try:
        window = sensor.buffer.window(
            end=now,
            duration_s=sensor.policy.clip_window_s,
            max_images=sensor.policy.max_images,
            max_age_s=sensor.policy.max_frame_age_s,
        )
    except ValueError:
        sensor._answer(look, 'failed', reason='fresh_rgb_unavailable')
        return
    request = CloudFallRequest(
        request_id=request_id,
        purpose='incident',
        device_id=sensor.device_id,
        boot_id=sensor.boot_id,
        incident_id=None,
        subject_key=look.track,
        evidence_revision=1,
        window=window,
        sensors=None,
        target=sensor._subjects.target(look.track, window) if look.track else None,
    )
    reply, error = await call_provider(
        sensor, request_id, 'incident', sensor._provider.analyze(request),
    )
    if reply is None:
        answer_failed(sensor, look, error)
        return
    if not isinstance(reply, CloudFallReply):
        sensor._answer(look, 'failed', reason='cloud_failed_or_invalid_response')
        return
    explain_look(sensor, look.look_id, request_id, 'incident', reply.assessment, reply.explanation)
    # The shared three questions: not a person, only an image, or ordinary all
    # come back as normal_activity; the rules never drop a fall motion for it.
    sensor._answer(
        look,
        'ordinary' if reply.assessment is VideoAssessment.NORMAL_ACTIVITY else 'suspect',
    )


async def run_arrival(sensor: Any, look: Look, request_id: str, now: float) -> None:
    """Compare what is present upon arrival with the initial suspicion photo."""
    after = now_window(sensor, now, ARRIVAL_WINDOW_S, ARRIVAL_FRAMES)
    if after is None:
        sensor._answer(look, 'failed', reason='fresh_rgb_unavailable')
        return
    before, box = photo_window(sensor, look.photo)
    if before is None:
        # No first sight (a restart): only whether a person is there now (design 6).
        reply, error = await call_provider(
            sensor,
            request_id,
            'person_check',
            sensor._provider.check_person(PersonCheckRequest(request_id, after)),
        )
        if reply is None:
            answer_failed(sensor, look, error)
            return
        verdict = {'person': 'person_unsure', 'not_person': 'gone'}.get(
            reply.verdict, 'not_visible',
        )
        explain_look(
            sensor, look.look_id, request_id, 'arrival_check', SHOWN[verdict],
            reply.explanation,
        )
        sensor._answer(look, verdict)
        return
    reply, error = await call_provider(
        sensor,
        request_id,
        'arrival_check',
        sensor._provider.check_arrival(
            ArrivalCheckRequest(request_id, before, box or look.box, after),
        ),
    )
    if reply is None:
        answer_failed(sensor, look, error)
        return
    explain_look(
        sensor, look.look_id, request_id, 'arrival_check', SHOWN[reply.verdict],
        reply.explanation,
    )
    sensor._answer(look, reply.verdict)


async def run_departure(sensor: Any, look: Look, request_id: str, now: float) -> None:
    """Review middle frames collected while waiting/driving to classify what left."""
    incident = sensor._incidents.get(look.incident_id)
    before, box = photo_window(sensor, look.photo)
    middle = list(incident.middle) if incident is not None else []
    after = now_window(sensor, now, 1.0, 1)
    if before is None or not middle or after is None:
        sensor._answer(look, 'not_seen')  # Nothing to see the leaving in (design 6).
        return
    if len(middle) > DEPARTURE_FRAMES:
        step = (len(middle) - 1) / (DEPARTURE_FRAMES - 1)
        middle = [middle[round(i * step)] for i in range(DEPARTURE_FRAMES)]
    window = FrameWindow(tuple(middle), middle[0].captured_at, middle[-1].captured_at, False)
    reply, error = await call_provider(
        sensor,
        request_id,
        'departure_check',
        sensor._provider.check_departure(
            DepartureCheckRequest(request_id, before, box or look.box, window, after),
        ),
    )
    if reply is None:
        answer_failed(sensor, look, error)
        return
    explain_look(
        sensor, look.look_id, request_id, 'departure_check', SHOWN[reply.verdict],
        reply.explanation,
    )
    sensor._answer(look, reply.verdict)


async def run_posture(sensor: Any, look: Look, request_id: str, now: float) -> None:
    """Ask Cloud to classify the subject's current posture and any fall moment."""
    window = now_window(sensor, now, POSTURE_WINDOW_S, POSTURE_FRAMES)
    if window is None:
        sensor._answer(look, 'failed', reason='fresh_rgb_unavailable')
        return
    moments = tuple(
        FrameWindow(m.frames, m.frames[0].captured_at, m.frames[-1].captured_at, False)
        for m in (sensor._moments.get(key) for key in look.fall_moments) if m is not None
    )
    reply, error = await call_provider(
        sensor,
        request_id,
        'posture_check',
        sensor._provider.check_posture(PostureCheckRequest(request_id, window, moments)),
    )
    if reply is None:
        answer_failed(sensor, look, error)
        return
    explain_look(
        sensor, look.look_id, request_id, 'posture_check', SHOWN[reply.verdict],
        reply.explanation,
    )
    # Fall moments asked but lost (a restart): the earlier moment is unknown (design 5).
    earlier = reply.earlier if moments else ('unknown' if look.fall_moments else None)
    sensor._answer(look, reply.verdict, earlier=earlier)


def on_furniture(
    sensor: Any,
    times: Sequence[float],
    finding: Any,
    regions: Sequence[Any],
) -> bool:
    """Every box rests clearly above the floor and no fall was seen (2026-10-09)."""
    locate3d = getattr(sensor._place, 'locate3d', None)
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


async def run_scan(sensor: Any) -> bool:
    """Cloud's periodic look at the whole scene: its finds become sightings."""
    now = sensor._now()
    if now < sensor._scan_anchor + sensor.periodic_interval_s():
        return False
    if sensor._guard_reason() is not None or sensor._rate_limited(now):
        sensor._scan_anchor = now
        return False
    blocked = sensor._provider_blocked()
    if blocked is not None:
        sensor._scan_anchor = now
        sensor._analysis = AnalysisStatus('failed', str(uuid4()), 'crosscheck', blocked)
        return False
    try:
        window = sensor.buffer.window(
            end=now,
            duration_s=sensor.policy.clip_window_s,
            max_images=sensor.policy.max_images,
            max_age_s=sensor.policy.max_frame_age_s,
        )
    except ValueError:
        return False
    if window.frames[-1].captured_at <= sensor._last_scan_end:
        return False
    sensor._scan_anchor = now
    sensor._last_scan_end = window.frames[-1].captured_at
    request = CloudFallRequest(
        request_id=str(uuid4()),
        purpose='crosscheck',
        device_id=sensor.device_id,
        boot_id=sensor.boot_id,
        incident_id=None,
        subject_key=None,
        evidence_revision=0,
        window=window,
        sensors=None,
    )
    reply, _ = await call_provider(
        sensor, request.request_id, 'crosscheck', sensor._provider.analyze(request),
    )
    if not isinstance(reply, CloudFallReply) or reply.assessment not in (
        VideoAssessment.OBSERVED_FALL, VideoAssessment.SUSPECTED_FALL,
    ):
        return True
    times = tuple(f.captured_at for f in window.frames)
    for finding in reply.findings:
        regions = [r for r in finding.regions if r.frame_index < len(times)]
        if on_furniture(sensor, times, finding, regions):
            continue
        region = regions[-1] if regions else None
        at = times[region.frame_index] if region is not None else times[-1]
        box = region.box if region is not None else None
        point = None
        if box is not None and sensor._place is not None:
            try:
                point = sensor._place.locate(at, box)
            except Exception:
                point = None
        moment = None
        if finding.assessment is VideoAssessment.OBSERVED_FALL:
            moment = 'm-' + uuid4().hex  # The frames Cloud judged a fall (design 5).
            sensor._moments[moment] = Photo(window.frames, None, at)
        frame = next((f for f in window.frames if f.captured_at == at), window.frames[-1])
        sighting = sensor._sight(
            'cloud', at, box=box, point=point, photo=True,
            frames=(frame,), fall_moment=moment, evidence=(times[0], times[-1]),
        )
        # Shown when the rules open an incident on it (look_id = the sighting).
        explain_look(
            sensor, sighting, request.request_id, 'crosscheck',
            finding.assessment, reply.explanation,
        )
    return True
