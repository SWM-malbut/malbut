"""No provider calls, ROS, playback or guardian delivery in these tests."""

import asyncio
from dataclasses import replace

import pytest

from malbut_agent_server.application.cloud_fall_monitor import CloudFallMonitor
from malbut_agent_server.application.fall_frame_buffer import FallFrameBuffer
from malbut_agent_server.domain.fall_monitoring import (
    AgentCheckReply, CandidateKind, CloudFallReply, FallCandidate, FallRuntimePolicy,
    IncidentState, RgbFrame, SensorSummary, VideoAssessment, VoiceAnswer,
    NotificationLevel, PersonObservation, PersonVisibility,
)


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


class Provider:
    execution_target = 'cloud'

    def __init__(self):
        self.calls = []
        self.reply = CloudFallReply(VideoAssessment.NORMAL_ACTIVITY, '관측')
        self.error = None
        self.started = asyncio.Event()
        self.release = None

    async def analyze(self, request):
        self.calls.append(request)
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        return self.reply


def policy():
    # Non-agreed fields below are test fixtures, NOT operating defaults.
    return FallRuntimePolicy.agreed(
        retry_interval_s=3, max_person_observation_age_s=2, clip_window_s=10,
        max_frame_age_s=2, max_calls_per_minute=20,
        max_incidents=10, max_images=12)


def frame(t):
    return RgbFrame(t, b'\xff\xd8test\xff\xd9')


def make(**changes):
    clock, provider = Clock(), Provider()
    monitor = CloudFallMonitor(
        device_id='robot', boot_id='boot-1', policy=replace(policy(), **changes),
        buffer=FallFrameBuffer(retention_s=30, max_bytes=100000, max_frames=100),
        provider=provider, clock=clock)
    return monitor, clock, provider


def enable(monitor, consent=True, connected=True):
    monitor.configure(enabled=True, camera_enabled=True,
                      cloud_consent=consent, connected=connected)


def candidate(t=100, subject='person-1', cid='c1', change=False):
    return FallCandidate(cid, subject, 'yolo_pose', CandidateKind.MOTION_SEEN,
                         t, significant_change=change)


def kinds(monitor):
    return [e.kind for e in monitor.drain_events()]


def answer(monitor, iid, value, *, qid=None, subject=None, revision=None, played=True):
    # Legacy voice-boundary regression tests explicitly request their question.
    # Production candidate ingestion now waits for VLM before requesting it.
    if monitor.incident(iid).question_id is None and qid is None:
        monitor.ask_question(iid)
    incident = monitor.incident(iid)
    return monitor.agent_reply(AgentCheckReply(
        iid, qid or incident.question_id, subject or incident.subject_key,
        revision if revision is not None else incident.revision, value, played))


def observe(monitor, clock, visibility):
    return monitor.observe_person(PersonObservation(clock(), visibility))


def test_agreed_settings_include_adaptive_intervals():
    assert (policy().scan_interval_s, policy().cloud_timeout_s,
            policy().max_rechecks) == (60, 20, 2)
    assert (policy().idle_scan_interval_s, policy().person_hold_s) == (300, 120)
    with pytest.raises(TypeError):
        FallRuntimePolicy.agreed()


@pytest.mark.parametrize('name,value', [
    ('scan_interval_s', 0), ('cloud_timeout_s', float('nan')),
    ('max_rechecks', True), ('max_rechecks', -1), ('max_images', 0),
    ('max_calls_per_minute', 0), ('person_hold_s', -1),
    ('idle_scan_interval_s', 30), ('max_person_observation_age_s', 0),
])
def test_policy_rejects_invalid_settings(name, value):
    with pytest.raises(ValueError):
        replace(policy(), **{name: value})


def test_defaults_off_and_local_provider_rejected():
    monitor, clock, provider = make()
    assert not monitor.ingest_rgb(frame(clock()))
    assert monitor.candidate(candidate()) is None
    assert not asyncio.run(monitor.run_once())
    provider.execution_target = 'local'
    with pytest.raises(ValueError, match='local'):
        CloudFallMonitor(device_id='r', boot_id='b', policy=policy(),
                         buffer=monitor.buffer, provider=provider)


@pytest.mark.parametrize('consent,connected,reason', [
    (False, True, 'cloud_consent_missing'),
    (True, False, 'cloud_disconnected'),
])
def test_common_gate_blocks_both_paths_before_vlm(consent, connected, reason):
    async def scenario():
        monitor, clock, provider = make()
        enable(monitor, consent, connected)
        monitor.ingest_rgb(frame(clock()))
        iid = monitor.candidate(candidate())
        assert 'question_requested' not in kinds(monitor)
        assert await monitor.run_once()
        assert monitor.incident(iid).last_failure == reason
        clock.value += 60
        monitor.ingest_rgb(frame(clock()))
        assert await monitor.run_once()
        assert any(e.kind == 'crosscheck_skipped' and e.reason == reason
                   for e in monitor.drain_events())
        assert provider.calls == []
    asyncio.run(scenario())


def test_periodic_scan_is_independent_of_candidates_and_runs_every_sixty_seconds():
    async def scenario():
        monitor, clock, provider = make()
        enable(monitor)
        monitor.ingest_rgb(frame(clock()))
        assert not await monitor.run_once()
        clock.value = 159
        monitor.ingest_rgb(frame(clock()))
        assert not await monitor.run_once()
        clock.value = 160
        monitor.ingest_rgb(frame(clock()))
        assert await monitor.run_once()
        assert provider.calls[0].purpose == 'crosscheck'
        assert provider.calls[0].incident_id is None
        assert 'crosscheck_completed' in kinds(monitor)
        assert not await monitor.run_once()
    asyncio.run(scenario())


def test_incident_priority_same_subject_merge_and_other_subject_separation():
    async def scenario():
        monitor, clock, provider = make()
        enable(monitor)
        clock.value += 60
        monitor.ingest_rgb(frame(clock()))
        a = monitor.candidate(candidate(clock()))
        assert monitor.candidate(candidate(clock(), cid='repeat')) == a
        b = monitor.candidate(candidate(clock(), subject='person-2', cid='c2'))
        assert a != b
        assert await monitor.run_once()
        assert provider.calls[0].incident_id == a
        assert await monitor.run_once()
        assert provider.calls[1].incident_id == b
        assert await monitor.run_once()
        assert provider.calls[2].purpose == 'crosscheck'
    asyncio.run(scenario())


def test_help_request_does_not_wait_for_cloud_or_duplicate_notification():
    async def scenario():
        monitor, clock, provider = make()
        enable(monitor)
        monitor.ingest_rgb(frame(clock()))
        iid = monitor.candidate(candidate())
        qid = monitor.ask_question(iid)  # Explicit legacy/operator check.
        provider.release = asyncio.Event()
        work = asyncio.create_task(monitor.run_once())
        await provider.started.wait()
        assert not await monitor.run_once()
        assert not answer(monitor, iid, VoiceAnswer.HELP, qid='wrong-question')
        assert answer(monitor, iid, VoiceAnswer.HELP, qid=qid)
        assert answer(monitor, iid, VoiceAnswer.HELP, qid=qid)
        notices = [e for e in monitor.drain_events() if e.kind == 'notification_requested']
        assert len(notices) == 1
        assert notices[0].notification_level is NotificationLevel.URGENT
        assert monitor.incident(iid).state is IncidentState.HELP_REQUIRED
        provider.release.set()
        await work
        assert monitor.incident(iid).state is IncidentState.HELP_REQUIRED
    asyncio.run(scenario())


def test_only_agent_can_report_no_response_and_notifies_while_cloud_pending():
    async def scenario():
        monitor, clock, provider = make()
        enable(monitor)
        monitor.ingest_rgb(frame(clock()))
        iid = monitor.candidate(candidate())
        qid = monitor.ask_question(iid)  # Explicit legacy/operator check.
        clock.value += 100
        assert monitor.incident(iid).answer is None
        provider.release = asyncio.Event()
        monitor.ingest_rgb(frame(clock()))
        task = asyncio.create_task(monitor.run_once())
        await provider.started.wait()
        clock.value += 9
        assert monitor.incident(iid).answer is None
        answer(monitor, iid, VoiceAnswer.NO_RESPONSE, qid=qid)
        assert monitor.incident(iid).answer is VoiceAnswer.NO_RESPONSE
        notices = [e for e in monitor.drain_events() if e.kind == 'notification_requested']
        assert len(notices) == 1
        assert notices[0].notification_level is NotificationLevel.CHECK
        assert notices[0].reason == 'person_no_response'
        provider.release.set()
        await task
        assert monitor.incident(iid).state is IncidentState.RECHECK_REQUIRED
    asyncio.run(scenario())


def test_revocation_discards_inflight_result_even_if_granted_again():
    async def scenario():
        monitor, clock, provider = make()
        enable(monitor)
        monitor.ingest_rgb(frame(clock()))
        iid = monitor.candidate(candidate())
        provider.release = asyncio.Event()
        work = asyncio.create_task(monitor.run_once())
        await provider.started.wait()
        enable(monitor, consent=False)
        enable(monitor, consent=True)
        provider.release.set()
        await work
        assert monitor.incident(iid).video is None
        assert monitor.incident(iid).last_failure == 'cloud_permission_changed'
        assert monitor.incident(iid).state is not IncidentState.RESOLVED
    asyncio.run(scenario())


@pytest.mark.parametrize('reply,error,expected', [
    (None, None, 'cloud_failed_or_invalid_response'),
    (None, RuntimeError('credential-must-not-leak'), 'cloud_failed_or_invalid_response'),
])
def test_invalid_or_failed_provider_is_not_normal_and_exception_not_leaked(reply, error, expected):
    async def scenario():
        monitor, clock, provider = make()
        enable(monitor)
        monitor.ingest_rgb(frame(clock()))
        iid = monitor.candidate(candidate())
        provider.reply, provider.error = reply, error
        await monitor.run_once()
        assert monitor.incident(iid).video is None
        assert monitor.incident(iid).last_failure == expected
        assert 'credential-must-not-leak' not in repr(monitor.drain_events())
    asyncio.run(scenario())


def test_timeout_does_not_start_local_fallback():
    async def scenario():
        monitor, clock, provider = make(cloud_timeout_s=0.01)
        enable(monitor)
        monitor.ingest_rgb(frame(clock()))
        iid = monitor.candidate(candidate())
        provider.release = asyncio.Event()
        await monitor.run_once()
        assert monitor.incident(iid).last_failure == 'cloud_timeout'
        assert len(provider.calls) == 1
        await asyncio.sleep(0)
    asyncio.run(scenario())


def test_new_change_during_call_queues_recheck_and_old_normal_cannot_clear_it():
    async def scenario():
        monitor, clock, provider = make()
        enable(monitor)
        monitor.ingest_rgb(frame(clock()))
        iid = monitor.candidate(candidate())
        provider.release = asyncio.Event()
        task = asyncio.create_task(monitor.run_once())
        await provider.started.wait()
        clock.value += 1
        monitor.ingest_rgb(frame(clock()))
        assert monitor.candidate(candidate(clock(), cid='new-drop', change=True)) == iid
        provider.release.set()
        await task
        assert monitor.incident(iid).video is None
        assert monitor.incident(iid).pending
        assert monitor.incident(iid).revision == 2
        assert 'stale_analysis_result' in kinds(monitor)
        clock.value += 3
        monitor.ingest_rgb(frame(clock()))
        assert await monitor.run_once()
        assert provider.calls[-1].evidence_revision == 2
        assert monitor.incident(iid).rechecks == 1
    asyncio.run(scenario())


def test_recheck_budget_new_rgb_and_failure_never_auto_resolve():
    async def scenario():
        monitor, clock, provider = make()
        enable(monitor)
        monitor.ingest_rgb(frame(clock()))
        iid = monitor.candidate(candidate())
        await monitor.run_once()
        monitor.request_recheck(iid)
        assert not await monitor.run_once()
        assert monitor.incident(iid).rechecks == 0
        for _ in range(2):
            clock.value += 3
            monitor.ingest_rgb(frame(clock()))
            monitor.request_recheck(iid)
            await monitor.run_once()
        assert len(provider.calls) == 3
        assert monitor.incident(iid).rechecks == 2
        assert not monitor.request_recheck(iid)
        answer(monitor, iid, VoiceAnswer.UNCLEAR)
        monitor.mark_unresolved(iid, revision=1, suspicion_persists=True)
        monitor.mark_unresolved(iid, revision=1, suspicion_persists=True)
        notices = [e for e in monitor.drain_events() if e.kind == 'notification_requested']
        assert len(notices) == 1
        assert notices[0].reason == 'check_required_not_confirmed_fall'
        assert monitor.incident(iid).state is IncidentState.RECHECK_REQUIRED
    asyncio.run(scenario())


def test_cloud_normal_and_matching_answer_require_new_video_and_subject_check():
    async def scenario():
        monitor, clock, provider = make()
        enable(monitor)
        monitor.ingest_rgb(frame(clock()))
        iid = monitor.candidate(candidate())
        await monitor.run_once()
        with pytest.raises(ValueError):
            monitor.resolve(iid, revision=1, reason='normal_verified')
        answer(monitor, iid, VoiceAnswer.OKAY)
        with pytest.raises(ValueError):
            monitor.resolve(iid, revision=1, reason='normal_verified')
        assert monitor.incident(iid).pending
        assert monitor.incident(iid).state is IncidentState.RECHECK_REQUIRED
    asyncio.run(scenario())


def test_recorded_fall_survives_later_normal_and_camera_off():
    async def scenario():
        monitor, clock, provider = make()
        enable(monitor)
        monitor.ingest_rgb(frame(clock()))
        iid = monitor.candidate(candidate())
        provider.reply = CloudFallReply(VideoAssessment.OBSERVED_FALL, '넘어짐')
        await monitor.run_once()
        clock.value += 3
        monitor.ingest_rgb(frame(clock()))
        monitor.request_recheck(iid)
        provider.reply = CloudFallReply(VideoAssessment.NORMAL_ACTIVITY, '회복처럼 보임')
        await monitor.run_once()
        assert monitor.incident(iid).fall_seen
        answer(monitor, iid, VoiceAnswer.OKAY)
        with pytest.raises(ValueError):
            monitor.resolve(iid, revision=1, reason='normal_verified')
        monitor.configure(enabled=True, camera_enabled=False, cloud_consent=True, connected=True)
        assert monitor.buffer.stored_bytes == 0
        assert monitor.incident(iid).state is not IncidentState.RESOLVED
        assert not await monitor.run_once()
    asyncio.run(scenario())


def test_global_call_limit_applies_to_periodic_and_incident_together():
    async def scenario():
        monitor, clock, provider = make(max_calls_per_minute=1)
        enable(monitor)
        clock.value += 60
        monitor.ingest_rgb(frame(clock()))
        await monitor.run_once()
        iid = monitor.candidate(candidate(clock()))
        await monitor.run_once()
        assert monitor.incident(iid).last_failure == 'cloud_rate_limit'
        assert len(provider.calls) == 1
    asyncio.run(scenario())


def test_invalid_sensor_data_and_future_candidate_rejected():
    with pytest.raises(ValueError):
        SensorSummary(0, floor_distance_m=float('nan'))
    with pytest.raises(ValueError):
        SensorSummary(0, floor_distance_m=-1)
    monitor, clock, _ = make()
    enable(monitor)
    with pytest.raises(ValueError):
        monitor.candidate(candidate(clock() + 1))
    with pytest.raises(ValueError):
        monitor.ingest_rgb(frame(clock() + 1))


def test_stale_candidate_and_capacity_are_explicit_not_silent():
    monitor, clock, _ = make(max_incidents=1)
    enable(monitor)
    assert monitor.candidate(candidate(clock() - 3)) is None
    assert monitor.candidate(candidate()) is not None
    assert monitor.candidate(candidate(subject='other')) is None
    reasons = [e.reason for e in monitor.drain_events() if e.kind == 'candidate_rejected']
    assert reasons == ['stale_candidate', 'incident_capacity']


def test_provider_ignoring_cancellation_cannot_overlap_or_apply_late_normal():
    async def scenario():
        monitor, clock, provider = make(cloud_timeout_s=0.01)
        enable(monitor)
        monitor.ingest_rgb(frame(clock()))
        iid = monitor.candidate(candidate())
        release = asyncio.Event()
        ignored = asyncio.Event()

        async def stubborn(request):
            provider.calls.append(request)
            try:
                await release.wait()
            except asyncio.CancelledError:
                ignored.set()
                await release.wait()
            return provider.reply

        provider.analyze = stubborn
        await monitor.run_once()
        await ignored.wait()
        monitor.request_recheck(iid)
        clock.value += 3
        monitor.ingest_rgb(frame(clock()))
        assert not await monitor.run_once()
        assert len(provider.calls) == 1
        release.set()
        await asyncio.sleep(0)
        assert monitor.incident(iid).video is None
        assert monitor.incident(iid).last_failure == 'cloud_timeout'
    asyncio.run(scenario())


def test_broken_non_async_adapter_is_reported_not_raised_from_worker():
    async def scenario():
        monitor, clock, provider = make()
        enable(monitor)
        monitor.ingest_rgb(frame(clock()))
        iid = monitor.candidate(candidate())
        provider.analyze = lambda request: None
        assert await monitor.run_once()
        assert monitor.incident(iid).last_failure == 'cloud_failed_or_invalid_response'
    asyncio.run(scenario())


def test_no_new_frames_do_not_consume_recheck_budget():
    async def scenario():
        monitor, clock, provider = make(max_frame_age_s=10)
        enable(monitor)
        monitor.ingest_rgb(frame(clock()))
        iid = monitor.candidate(candidate())
        await monitor.run_once()
        monitor.request_recheck(iid)
        clock.value += 3
        assert not await monitor.run_once()
        assert len(provider.calls) == 1
        assert monitor.incident(iid).rechecks == 0
        assert monitor.incident(iid).pending
    asyncio.run(scenario())


def test_unanswered_question_blocks_unresolved_verdict_and_stale_decision_rejected():
    async def scenario():
        monitor, clock, provider = make(max_rechecks=0)
        enable(monitor)
        monitor.ingest_rgb(frame(clock()))
        iid = monitor.candidate(candidate())
        await monitor.run_once()
        with pytest.raises(ValueError, match='pending'):
            monitor.mark_unresolved(iid, revision=1, suspicion_persists=True)
        answer(monitor, iid, VoiceAnswer.FAILED)
        with pytest.raises(ValueError, match='stale'):
            monitor.mark_unresolved(iid, revision=0, suspicion_persists=True)
    asyncio.run(scenario())


def test_presence_hold_then_idle_without_sliding_scan_deadline():
    async def scenario():
        monitor, clock, provider = make()
        enable(monitor)
        observe(monitor, clock, PersonVisibility.SEEN)
        for t in range(101, 220):
            clock.value = t
            observe(monitor, clock, PersonVisibility.NOT_SEEN)
            monitor.ingest_rgb(frame(t))
            assert monitor.periodic_interval_s() == 60
            assert bool(await monitor.run_once()) == (t == 160)
        for t in range(220, 460):
            clock.value = t
            observe(monitor, clock, PersonVisibility.NOT_SEEN)
            assert monitor.periodic_interval_s() == 300
            assert not await monitor.run_once()
        clock.value = 460
        observe(monitor, clock, PersonVisibility.NOT_SEEN)
        monitor.ingest_rgb(frame(clock()))
        assert await monitor.run_once()
        assert len(provider.calls) == 2
    asyncio.run(scenario())


def test_reappearing_person_advances_scan_but_does_not_duplicate_it():
    async def scenario():
        monitor, clock, provider = make()
        enable(monitor)
        observe(monitor, clock, PersonVisibility.NOT_SEEN)
        clock.value = 180
        observe(monitor, clock, PersonVisibility.NOT_SEEN)
        assert not await monitor.run_once()
        clock.value = 181
        observe(monitor, clock, PersonVisibility.SEEN)
        monitor.ingest_rgb(frame(clock()))
        assert await monitor.run_once()
        for t in range(182, 241):
            clock.value = t
            observe(monitor, clock, PersonVisibility.SEEN)
            assert not await monitor.run_once()
        clock.value = 241
        observe(monitor, clock, PersonVisibility.SEEN)
        monitor.ingest_rgb(frame(clock()))
        assert await monitor.run_once()
        assert len(provider.calls) == 2
    asyncio.run(scenario())


def test_never_seen_person_still_gets_periodic_check_and_candidates_bypass_wait():
    async def scenario():
        monitor, clock, provider = make()
        enable(monitor)
        for t in (100, 399):
            clock.value = t
            observe(monitor, clock, PersonVisibility.NOT_SEEN)
            assert not await monitor.run_once()
        clock.value = 400
        observe(monitor, clock, PersonVisibility.NOT_SEEN)
        monitor.ingest_rgb(frame(clock()))
        assert await monitor.run_once()
        assert provider.calls[-1].purpose == 'crosscheck'
        iid = monitor.candidate(candidate(clock()))
        assert await monitor.run_once()
        assert provider.calls[-1].incident_id == iid
        assert monitor.periodic_interval_s() == 60  # unresolved incident
    asyncio.run(scenario())


def test_missing_failed_or_stale_detector_is_not_absence():
    monitor, clock, _ = make()
    enable(monitor)
    assert monitor.periodic_interval_s() == 60
    observe(monitor, clock, PersonVisibility.NOT_SEEN)
    assert monitor.periodic_interval_s() == 300
    clock.value += 3
    assert monitor.periodic_interval_s() == 60
    observe(monitor, clock, PersonVisibility.UNKNOWN)
    assert monitor.periodic_interval_s() == 60


def test_presence_rejects_future_stale_reordered_and_disabled_input():
    monitor, clock, _ = make()
    assert not observe(monitor, clock, PersonVisibility.NOT_SEEN)
    enable(monitor)
    observe(monitor, clock, PersonVisibility.SEEN)
    assert not observe(monitor, clock, PersonVisibility.NOT_SEEN)
    assert not monitor.observe_person(PersonObservation(99, PersonVisibility.NOT_SEEN))
    with pytest.raises(ValueError, match='future'):
        monitor.observe_person(PersonObservation(101, PersonVisibility.NOT_SEEN))
    clock.value = 105
    assert not monitor.observe_person(PersonObservation(101, PersonVisibility.NOT_SEEN))
    monitor.configure(enabled=True, camera_enabled=False, cloud_consent=True, connected=True)
    enable(monitor)
    assert monitor.periodic_interval_s() == 60


def test_agent_no_response_requires_playback_and_failures_do_not_imply_no_response():
    monitor, _, _ = make()
    enable(monitor)
    iid = monitor.candidate(candidate())
    with pytest.raises(ValueError, match='played question'):
        answer(monitor, iid, VoiceAnswer.NO_RESPONSE, played=False)
    assert answer(monitor, iid, VoiceAnswer.FAILED, played=False)
    events = monitor.drain_events()
    assert any(e.kind == 'agent_check_failed' for e in events)
    assert not any(e.kind == 'notification_requested' for e in events)
    assert monitor.incident(iid).answer is VoiceAnswer.FAILED


def test_wrong_subject_and_old_revision_cannot_supply_answers():
    monitor, clock, _ = make()
    enable(monitor)
    iid = monitor.candidate(candidate())
    old_question = monitor.ask_question(iid)
    assert not answer(monitor, iid, VoiceAnswer.OKAY, subject='other-person')
    clock.value += 1
    monitor.candidate(candidate(clock(), cid='new', change=True))
    assert monitor.incident(iid).question_id != old_question
    assert not answer(monitor, iid, VoiceAnswer.OKAY, qid=old_question, revision=1)
    assert not answer(monitor, iid, VoiceAnswer.OKAY, revision=1)
    assert monitor.incident(iid).answer is None


@pytest.mark.parametrize('answer_first', [True, False])
def test_fall_plus_okay_keeps_record_and_requests_info_without_normal_closure(answer_first):
    async def scenario():
        monitor, clock, provider = make()
        enable(monitor)
        monitor.ingest_rgb(frame(clock()))
        iid = monitor.candidate(candidate())
        provider.reply = CloudFallReply(VideoAssessment.OBSERVED_FALL, '넘어짐')
        if answer_first:
            answer(monitor, iid, VoiceAnswer.OKAY)
        await monitor.run_once()
        if not answer_first:
            answer(monitor, iid, VoiceAnswer.OKAY)
        assert monitor.incident(iid).fall_seen
        assert monitor.incident(iid).state is not IncidentState.RESOLVED
        notices = [e for e in monitor.drain_events() if e.kind == 'notification_requested']
        assert len(notices) == 1
        assert notices[0].notification_level is NotificationLevel.INFO
        assert notices[0].reason == 'fall_observed_person_okay'
        with pytest.raises(ValueError):
            monitor.resolve(iid, revision=1, reason='normal_verified')
    asyncio.run(scenario())


def test_notification_escalates_without_duplicate_or_later_downgrade():
    async def scenario():
        monitor, clock, provider = make()
        enable(monitor)
        monitor.ingest_rgb(frame(clock()))
        iid = monitor.candidate(candidate())
        provider.reply = CloudFallReply(VideoAssessment.OBSERVED_FALL, '넘어짐')
        await monitor.run_once()
        answer(monitor, iid, VoiceAnswer.OKAY)
        monitor.ask_question(iid)
        answer(monitor, iid, VoiceAnswer.NO_RESPONSE)
        answer(monitor, iid, VoiceAnswer.HELP)
        answer(monitor, iid, VoiceAnswer.HELP)
        monitor.ask_question(iid)
        answer(monitor, iid, VoiceAnswer.OKAY)
        levels = [e.notification_level for e in monitor.drain_events()
                  if e.kind == 'notification_requested']
        assert levels == [NotificationLevel.INFO, NotificationLevel.CHECK, NotificationLevel.URGENT]
        assert monitor.incident(iid).notification_level is NotificationLevel.URGENT
        assert monitor.incident(iid).fall_seen
    asyncio.run(scenario())
