"""A Pose case whose Cloud check fails is sent again, then asked about anyway."""

import asyncio

import pytest

from malbut_agent_server.domain.fall_monitoring import (
    CloudFallReply, IncidentState, VideoAssessment,
)
from malbut_agent_server.fall_runtime import event_metadata
from malbut_agent_server.ports.cloud_fall import CloudFallProviderError
from test_cloud_fall_monitor import Provider, candidate, enable, frame, make

SUSPECTED = CloudFallReply(VideoAssessment.SUSPECTED_FALL, '바닥에 누운 사람이 보입니다.')
NORMAL = CloudFallReply(VideoAssessment.NORMAL_ACTIVITY, '스스로 움직입니다.')


class Flaky(Provider):
    """Fails the first `failures` incident checks, then answers `reply`."""

    def __init__(self, failures, reply=SUSPECTED, error=None):
        super().__init__()
        self.failures, self.reply = failures, reply
        self.failure = error or RuntimeError('cloud down')

    async def analyze(self, request):
        self.calls.append(request)
        if request.purpose == 'incident' and self.failures:
            self.failures -= 1
            raise self.failure
        return self.reply


def opened(provider):
    monitor, clock, _ = make()
    monitor._provider = provider
    enable(monitor)
    monitor.ingest_rgb(frame(clock()))
    iid = monitor.candidate(candidate())
    return monitor, clock, iid


def attempts(monitor, clock, count):
    events = []
    for _ in range(count):
        asyncio.run(monitor.run_once())
        events += list(monitor.drain_events())
        clock.value += 3
        monitor.ingest_rgb(frame(clock()))
    return events


def checks(provider):
    return [c for c in provider.calls if c.purpose == 'incident']


def of(events, kind):
    return [e for e in events if e.kind == kind]


def test_a_failed_check_is_sent_again_and_the_later_answer_is_used():
    provider = Flaky(2)
    monitor, clock, iid = opened(provider)
    events = attempts(monitor, clock, 3)
    assert len(checks(provider)) == 3
    assert len(of(events, 'analysis_unavailable')) == 2
    question, = of(events, 'question_requested')
    assert question.reply.assessment is VideoAssessment.SUSPECTED_FALL


@pytest.mark.parametrize('error,reason', [
    (RuntimeError('cloud down'), 'cloud_failed_or_invalid_response'),
    (CloudFallProviderError('cloud_http_error'), 'cloud_http_error'),
])
def test_three_failures_ask_the_person_on_the_pose_evidence(error, reason):
    provider = Flaky(5, error=error)
    monitor, clock, iid = opened(provider)
    events = attempts(monitor, clock, 5)
    assert len(checks(provider)) == 3  # The first check and two retries.
    assert [e.reason for e in of(events, 'analysis_unavailable')] == [reason] * 3
    question, = of(events, 'question_requested')
    assert question.subject_key == 'person-1'
    assert event_metadata(question)['video_assessment'] == 'unobservable'
    incident = monitor.incident(iid)
    assert incident.last_failure == reason and incident.auto_normal_blocked
    assert incident.state is not IncidentState.RESOLVED
    replay, = monitor.pending_questions()
    assert replay.question_id == question.question_id


def test_a_timed_out_check_is_sent_again():
    async def scenario():
        monitor, clock, provider = make(cloud_timeout_s=0.01)
        enable(monitor)
        monitor.ingest_rgb(frame(clock()))
        iid = monitor.candidate(candidate())
        provider.release = asyncio.Event()  # Never answers.
        await monitor.run_once()
        assert monitor.incident(iid).last_failure == 'cloud_timeout'
        assert monitor.incident(iid).pending
        await asyncio.sleep(0)  # Let the cancelled call finish.
        clock.value += 3
        monitor.ingest_rgb(frame(clock()))
        await monitor.run_once()
        assert len(provider.calls) == 2
    asyncio.run(scenario())


def test_a_retry_that_comes_back_normal_closes_nothing_and_asks_nothing():
    provider = Flaky(1, reply=NORMAL)
    monitor, clock, iid = opened(provider)
    events = attempts(monitor, clock, 2)
    assert len(checks(provider)) == 2
    assert not of(events, 'question_requested')
    completed, = of(events, 'analysis_completed')
    assert completed.reply.assessment is VideoAssessment.NORMAL_ACTIVITY


@pytest.mark.parametrize('guard', ['consent', 'disconnected', 'mapping'])
def test_consent_connection_and_settings_guards_neither_retry_nor_ask(guard):
    provider = Flaky(0)
    monitor, clock, iid = opened(provider)
    if guard == 'mapping':
        monitor.set_cloud_block('mapping')
    else:
        enable(monitor, consent=guard != 'consent', connected=guard != 'disconnected')
    events = list(monitor.drain_events()) + attempts(monitor, clock, 3)
    assert not checks(provider)
    assert len(of(events, 'analysis_unavailable')) == 1
    assert not of(events, 'question_requested')
    assert not monitor.incident(iid).pending


def test_a_case_already_asked_is_not_asked_again_after_a_failed_recheck():
    provider = Flaky(0)
    monitor, clock, iid = opened(provider)
    first, = of(attempts(monitor, clock, 1), 'question_requested')
    provider.failures = 5
    assert monitor.request_recheck(iid)
    events = attempts(monitor, clock, 3)
    assert len(checks(provider)) == 2
    assert not of(events, 'question_requested')
    assert monitor.incident(iid).question_id == first.question_id
