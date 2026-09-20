"""State-machine checks only: fake provider/Agent/subject checks, no accuracy claim."""

import asyncio
from dataclasses import replace

import pytest

from malbut_agent_server.domain.fall_monitoring import (
    CloudFallReply, IncidentState, PersonObservation, PersonVisibility,
    SubjectCheckState, SubjectObservation, VideoAssessment, VoiceAnswer,
)
from test_cloud_fall_monitor import answer, candidate, enable, frame, make


def subject_check(monitor, iid, clock, **changes):
    incident = monitor.incident(iid)
    check = SubjectObservation(
        iid, incident.subject_key, incident.revision,
        incident.normal_checks[-1].request_id, clock(), SubjectCheckState.CLEAR, True)
    return replace(check, **changes)


async def two_checks(*, answer_early=True, source='yolo_pose'):
    monitor, clock, provider = make()
    enable(monitor)
    iid = monitor.candidate(replace(candidate(), source=source))
    monitor.ingest_rgb(frame(clock()))
    if answer_early:
        answer(monitor, iid, VoiceAnswer.OKAY)
    await monitor.run_once()
    assert monitor.incident(iid).state is not IncidentState.RESOLVED
    # Explicit check when there is no okay answer or the source is not YOLO.
    if not monitor.incident(iid).pending:
        monitor.request_recheck(iid)
    clock.value += 3
    monitor.ingest_rgb(frame(clock()))
    await monitor.run_once()
    return monitor, clock, provider, iid


@pytest.mark.parametrize('order', [
    'answer_before_video', 'answer_before_check', 'check_before_answer',
])
def test_auto_closure_only_after_two_normal_videos_valid_answer_and_new_subject_check(order):
    async def run():
        monitor, clock, provider, iid = await two_checks(
            answer_early=order == 'answer_before_video')
        assert len(provider.calls) == 2
        if order == 'answer_before_check':
            answer(monitor, iid, VoiceAnswer.OKAY)
        assert monitor.incident(iid).state is not IncidentState.RESOLVED
        observation = subject_check(monitor, iid, clock)
        assert monitor.observe_subject(observation)
        if order == 'check_before_answer':
            assert monitor.incident(iid).state is not IncidentState.RESOLVED
            answer(monitor, iid, VoiceAnswer.OKAY)
        incident = monitor.incident(iid)
        assert incident.state is IncidentState.RESOLVED
        assert incident.close_reason == 'normal_verified'
        assert incident.attempts == 2 and incident.rechecks == 1
        events = monitor.drain_events()
        assert events[-1].kind == 'incident_resolved'
        assert len([e for e in events if e.kind == 'incident_resolved']) == 1
        assert not any(e.kind == 'notification_requested' for e in events)
        assert len([e for e in events if e.kind == 'analysis_completed']) == 2
        # No duplicate close/event after a repeated observation or command.
        assert not monitor.observe_subject(observation)
        monitor.resolve(iid, revision=1, reason='normal_verified')
        assert monitor.drain_events() == ()
    asyncio.run(run())


def test_first_normal_schedules_bounded_recheck_but_duplicate_video_does_not_count():
    async def run():
        monitor, clock, provider = make(max_frame_age_s=10)
        enable(monitor)
        iid = monitor.candidate(candidate())
        monitor.ingest_rgb(frame(clock()))
        await monitor.run_once()
        answer(monitor, iid, VoiceAnswer.OKAY)
        assert monitor.incident(iid).pending
        assert monitor.observe_subject(subject_check(monitor, iid, clock))
        assert not await monitor.run_once()  # Retry spacing still applies.
        clock.value += 3
        assert not await monitor.run_once()  # Same RGB window is not a recheck.
        assert monitor.incident(iid).rechecks == 0
        assert len(provider.calls) == 1
        monitor.ingest_rgb(frame(clock()))
        assert await monitor.run_once()
        assert len(monitor.incident(iid).normal_checks) == 2
        # Old subject check references the initial request, not this new video.
        assert monitor.incident(iid).state is not IncidentState.RESOLVED
        with pytest.raises(ValueError, match='not supported'):
            monitor.resolve(iid, revision=1, reason='normal_verified')
    asyncio.run(run())


@pytest.mark.parametrize('changes', [
    {'incident_id': 'other'}, {'subject_key': 'other'}, {'evidence_revision': 2},
    {'request_id': 'other'}, {'observed_at': 100},
])
def test_wrong_or_stale_subject_observations_cannot_close(changes):
    async def run():
        monitor, clock, _, iid = await two_checks()
        assert not monitor.observe_subject(subject_check(monitor, iid, clock, **changes))
        assert monitor.incident(iid).state is not IncidentState.RESOLVED
    asyncio.run(run())


@pytest.mark.parametrize('visibility', list(PersonVisibility))
def test_scene_presence_or_absence_is_not_subject_clearance(visibility):
    async def run():
        monitor, clock, _, iid = await two_checks()
        assert monitor.observe_person(PersonObservation(clock(), visibility))
        assert monitor.incident(iid).state is not IncidentState.RESOLVED
        with pytest.raises(ValueError, match='not supported'):
            monitor.resolve(iid, revision=1, reason='normal_verified')
    asyncio.run(run())


@pytest.mark.parametrize('changes', [
    {'association_verified': False}, {'state': SubjectCheckState.UNKNOWN},
    {'state': SubjectCheckState.SUSPECTED},
])
def test_uncertain_or_new_suspicion_clears_old_normal_evidence(changes):
    async def run():
        monitor, clock, _, iid = await two_checks(answer_early=False)
        observation = subject_check(monitor, iid, clock, **changes)
        assert monitor.observe_subject(observation)
        answer(monitor, iid, VoiceAnswer.OKAY)
        assert monitor.incident(iid).normal_checks == ()
        assert monitor.incident(iid).state is not IncidentState.RESOLVED
        # A new clear message cannot revive a discarded request's evidence.
        assert not monitor.observe_subject(replace(
            observation, state=SubjectCheckState.CLEAR, association_verified=True))
    asyncio.run(run())


def test_subject_observation_must_still_be_fresh_when_answer_arrives():
    async def run():
        monitor, clock, _, iid = await two_checks(answer_early=False)
        assert monitor.observe_subject(subject_check(monitor, iid, clock))
        clock.value += 3
        answer(monitor, iid, VoiceAnswer.OKAY)
        assert monitor.incident(iid).state is not IncidentState.RESOLVED
        with pytest.raises(ValueError, match='not supported'):
            monitor.resolve(iid, revision=1, reason='normal_verified')
    asyncio.run(run())


@pytest.mark.parametrize('visibility', [PersonVisibility.UNKNOWN, PersonVisibility.NOT_SEEN])
def test_later_detector_loss_invalidates_existing_subject_clearance(visibility):
    async def run():
        monitor, clock, _, iid = await two_checks(answer_early=False)
        assert monitor.observe_subject(subject_check(monitor, iid, clock))
        clock.value += 0.1
        monitor.observe_person(PersonObservation(clock(), visibility))
        answer(monitor, iid, VoiceAnswer.OKAY)
        assert monitor.incident(iid).subject_observation is None
        assert monitor.incident(iid).state is not IncidentState.RESOLVED
    asyncio.run(run())


def test_future_observation_is_rejected_and_unplayed_okay_cannot_close():
    async def run():
        monitor, clock, _, iid = await two_checks(answer_early=False)
        with pytest.raises(ValueError, match='future'):
            monitor.observe_subject(subject_check(monitor, iid, clock, observed_at=104))
        assert monitor.observe_subject(subject_check(monitor, iid, clock))
        answer(monitor, iid, VoiceAnswer.OKAY, played=False)
        assert monitor.incident(iid).state is not IncidentState.RESOLVED
    asyncio.run(run())


@pytest.mark.parametrize('source', ['yolo_pose', 'cloud_crosscheck'])
def test_additional_suspicion_invalidates_normal_checks_even_without_revision_change(source):
    async def run():
        monitor, clock, _, iid = await two_checks(answer_early=False)
        old_observation = subject_check(monitor, iid, clock)
        clock.value += 1
        assert monitor.candidate(replace(candidate(clock(), cid='new'), source=source)) == iid
        assert monitor.incident(iid).normal_checks == ()
        assert not monitor.observe_subject(old_observation)
        answer(monitor, iid, VoiceAnswer.OKAY)
        assert monitor.incident(iid).state is not IncidentState.RESOLVED
    asyncio.run(run())


def test_cloud_originated_incident_cannot_use_yolo_only_closure():
    async def run():
        monitor, clock, _, iid = await two_checks(source='cloud_crosscheck')
        assert monitor.observe_subject(subject_check(monitor, iid, clock))
        assert monitor.incident(iid).state is not IncidentState.RESOLVED
        with pytest.raises(ValueError, match='not supported'):
            monitor.resolve(iid, revision=1, reason='normal_verified')
    asyncio.run(run())


@pytest.mark.parametrize('assessment', [
    VideoAssessment.OBSERVED_FALL, VideoAssessment.SUSPECTED_FALL, VideoAssessment.UNOBSERVABLE,
])
def test_earlier_cloud_non_normal_is_not_erased_by_later_normals(assessment):
    async def run():
        monitor, clock, provider = make()
        enable(monitor)
        iid = monitor.candidate(candidate())
        monitor.ingest_rgb(frame(clock()))
        provider.reply = CloudFallReply(assessment, '확인')
        await monitor.run_once()
        provider.reply = CloudFallReply(VideoAssessment.NORMAL_ACTIVITY, '새 확인')
        for _ in range(2):
            clock.value += 3
            monitor.ingest_rgb(frame(clock()))
            monitor.request_recheck(iid)
            await monitor.run_once()
        assert monitor.observe_subject(subject_check(monitor, iid, clock))
        answer(monitor, iid, VoiceAnswer.OKAY)
        assert monitor.incident(iid).auto_normal_blocked
        assert monitor.incident(iid).state is not IncidentState.RESOLVED
        assert monitor.incident(iid).fall_seen == (assessment is VideoAssessment.OBSERVED_FALL)
    asyncio.run(run())


@pytest.mark.parametrize('earlier_answer', [VoiceAnswer.HELP, VoiceAnswer.NO_RESPONSE])
def test_notification_history_blocks_closure_after_new_okay_answer(earlier_answer):
    async def run():
        monitor, clock, _, iid = await two_checks(answer_early=False)
        answer(monitor, iid, earlier_answer)
        monitor.ask_question(iid)
        assert monitor.observe_subject(subject_check(monitor, iid, clock))
        answer(monitor, iid, VoiceAnswer.OKAY)
        assert monitor.incident(iid).state is not IncidentState.RESOLVED
        assert len([e for e in monitor.drain_events() if e.kind == 'notification_requested']) == 1
        with pytest.raises(ValueError, match='not supported'):
            monitor.resolve(iid, revision=1, reason='normal_verified')
    asyncio.run(run())


def test_cloud_failure_is_not_a_successful_second_normal_check():
    async def run():
        monitor, clock, provider = make()
        enable(monitor)
        iid = monitor.candidate(candidate())
        answer(monitor, iid, VoiceAnswer.OKAY)
        monitor.ingest_rgb(frame(clock()))
        await monitor.run_once()
        provider.error = ValueError('invalid reply')
        clock.value += 3
        monitor.ingest_rgb(frame(clock()))
        await monitor.run_once()
        assert monitor.incident(iid).normal_checks == ()
        assert monitor.incident(iid).last_failure is not None
        provider.error = None
        clock.value += 3
        monitor.ingest_rgb(frame(clock()))
        monitor.request_recheck(iid)
        await monitor.run_once()
        assert monitor.observe_subject(subject_check(monitor, iid, clock))
        assert len(monitor.incident(iid).normal_checks) == 1
        assert monitor.incident(iid).rechecks == 2
        assert not monitor.incident(iid).pending
        assert monitor.incident(iid).state is not IncidentState.RESOLVED
    asyncio.run(run())


def test_new_revision_while_recheck_pending_rejects_old_normals_and_answer():
    async def run():
        monitor, clock, provider = make()
        enable(monitor)
        iid = monitor.candidate(candidate())
        answer(monitor, iid, VoiceAnswer.OKAY)
        monitor.ingest_rgb(frame(clock()))
        await monitor.run_once()
        old_question = monitor.incident(iid).question_id
        provider.started.clear()
        provider.release = asyncio.Event()
        clock.value += 3
        monitor.ingest_rgb(frame(clock()))
        task = asyncio.create_task(monitor.run_once())
        await provider.started.wait()
        clock.value += 1
        monitor.candidate(candidate(clock(), cid='changed', change=True))
        provider.release.set()
        await task
        assert monitor.incident(iid).normal_checks == ()
        assert monitor.incident(iid).revision == 2
        assert monitor.incident(iid).answer is None
        assert not answer(monitor, iid, VoiceAnswer.OKAY, qid=old_question, revision=1)
        assert monitor.incident(iid).state is not IncidentState.RESOLVED
    asyncio.run(run())


def test_camera_off_invalidates_clearance_and_reenable_does_not_restore_it():
    async def run():
        monitor, clock, _, iid = await two_checks(answer_early=False)
        old = subject_check(monitor, iid, clock)
        assert monitor.observe_subject(old)
        monitor.configure(enabled=True, camera_enabled=False, cloud_consent=True, connected=True)
        answer(monitor, iid, VoiceAnswer.OKAY)
        assert monitor.incident(iid).state is not IncidentState.RESOLVED
        assert not monitor.observe_subject(old)
        enable(monitor)
        assert not monitor.observe_subject(old)
        assert monitor.incident(iid).normal_checks == ()
    asyncio.run(run())


@pytest.mark.parametrize('assessment', [
    VideoAssessment.OBSERVED_FALL, VideoAssessment.SUSPECTED_FALL, VideoAssessment.UNOBSERVABLE,
])
def test_late_earlier_non_normal_result_blocks_auto_closure_and_preserves_fall(assessment):
    async def run():
        monitor, clock, provider = make()
        enable(monitor)
        iid = monitor.candidate(candidate())
        monitor.ingest_rgb(frame(clock()))
        provider.reply = CloudFallReply(assessment, '이전 영상')
        provider.release = asyncio.Event()
        task = asyncio.create_task(monitor.run_once())
        await provider.started.wait()
        clock.value += 1
        monitor.candidate(candidate(clock(), cid='change', change=True))
        provider.release.set()
        await task
        assert monitor.incident(iid).video is None
        assert monitor.incident(iid).fall_seen == (assessment is VideoAssessment.OBSERVED_FALL)
        provider.reply = CloudFallReply(VideoAssessment.NORMAL_ACTIVITY, '새 영상')
        for _ in range(2):
            clock.value += 3
            monitor.ingest_rgb(frame(clock()))
            monitor.request_recheck(iid)
            await monitor.run_once()
        monitor.observe_subject(subject_check(monitor, iid, clock))
        answer(monitor, iid, VoiceAnswer.OKAY)
        assert monitor.incident(iid).state is not IncidentState.RESOLVED
        assert monitor.incident(iid).auto_normal_blocked
    asyncio.run(run())
