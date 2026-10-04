"""VLM-first handoff and Manager final-result boundaries, without network or ROS."""

import asyncio
import json

import pytest

from malbut_agent_server.domain.fall_monitoring import (
    CloudFallReply, IncidentState, VideoAssessment, VoiceAnswer,
)
from malbut_agent_server.fall_runtime import apply_decision
from test_cloud_fall_monitor import answer, candidate, enable, frame, make


def assessed(assessment=VideoAssessment.SUSPECTED_FALL):
    monitor, clock, provider = make()
    enable(monitor)
    monitor.ingest_rgb(frame(clock()))
    iid = monitor.candidate(candidate())
    assert monitor.incident(iid).question_id is None
    assert not any(e.kind == 'question_requested' for e in monitor.drain_events())
    provider.reply = CloudFallReply(assessment, '비공개 모델 설명')
    asyncio.run(monitor.run_once())
    return monitor, clock, provider, iid


def result(monitor, iid, **changes):
    incident = monitor.incident(iid)
    return json.dumps(dict(
        action='confirmation_result', boot_id=monitor.boot_id,
        incident_id=iid, question_id=incident.question_id,
        subject_key=incident.subject_key, evidence_revision=incident.revision,
        situation_assessment='resolved', help_needed=False) | changes)


@pytest.mark.parametrize('assessment', [
    VideoAssessment.SUSPECTED_FALL, VideoAssessment.OBSERVED_FALL,
    VideoAssessment.UNOBSERVABLE,
])
def test_question_is_only_requested_after_current_vlm_assessment(assessment):
    monitor, _, _, iid = assessed(assessment)
    events = monitor.drain_events()
    assert [e.kind for e in events] == ['analysis_completed', 'question_requested']
    assert events[-1].reply.assessment == assessment
    assert monitor.incident(iid).question_id == events[-1].question_id
    assert monitor.pending_questions()[0].question_id == events[-1].question_id


def test_normal_video_does_not_question_and_manager_may_clear_it():
    monitor, _, _, iid = assessed(VideoAssessment.NORMAL_ACTIVITY)
    assert monitor.incident(iid).question_id is None
    assert not any(e.kind == 'question_requested' for e in monitor.drain_events())
    assert monitor.pending_questions()[0].kind == 'analysis_completed'
    apply_decision(monitor, json.dumps(dict(
        action='dismiss_normal', boot_id=monitor.boot_id, incident_id=iid, evidence_revision=1)))
    assert monitor.incident(iid).state is IncidentState.RESOLVED
    assert not monitor.pending_questions()


def test_user_lying_down_overrides_observed_fall_and_duplicate_is_idempotent():
    monitor, _, _, iid = assessed(VideoAssessment.OBSERVED_FALL)
    assert apply_decision(monitor, result(monitor, iid))
    incident = monitor.incident(iid)
    assert incident.state is IncidentState.RESOLVED
    assert incident.situation_assessment == 'resolved'
    assert incident.fall_seen  # Preserve evidence history, not its final conclusion.
    monitor.drain_events()
    assert apply_decision(monitor, result(monitor, iid))
    assert monitor.drain_events() == ()
    assert not apply_decision(monitor, result(
        monitor, iid, situation_assessment='unknown', help_needed=True))


def test_unknown_help_needed_does_not_invent_a_confirmed_fall():
    monitor, _, _, iid = assessed()
    assert apply_decision(monitor, result(
        monitor, iid, situation_assessment='unknown', help_needed=True))
    incident = monitor.incident(iid)
    assert incident.state is IncidentState.HELP_REQUIRED
    assert incident.situation_assessment == 'unknown'
    assert not incident.fall_seen
    assert not monitor.pending_questions()


@pytest.mark.parametrize('assessment,help_needed', [('resolved', True), ('unknown', False)])
def test_actual_situation_and_explicit_help_choice_are_independent(assessment, help_needed):
    monitor, _, _, iid = assessed()
    assert apply_decision(monitor, result(
        monitor, iid, situation_assessment=assessment, help_needed=help_needed))
    incident = monitor.incident(iid)
    assert incident.situation_assessment == assessment
    assert incident.help_needed is help_needed
    expected = IncidentState.HELP_REQUIRED if help_needed else IncidentState.RESOLVED
    assert incident.state is expected


def test_transport_failure_is_not_user_no_response_or_help_needed():
    monitor, _, _, iid = assessed()
    command = json.loads(result(monitor, iid))
    for key in ('subject_key', 'situation_assessment', 'help_needed'):
        command.pop(key)
    command['action'] = 'confirmation_failed'
    monitor.drain_events()
    assert apply_decision(monitor, json.dumps(command))
    assert monitor.incident(iid).help_needed is None
    assert monitor.incident(iid).state is IncidentState.RECHECK_REQUIRED
    assert [e.kind for e in monitor.drain_events()] == ['agent_check_failed']


def test_new_evidence_keeps_question_and_accepts_its_result_without_clearing_new_evidence():
    monitor, clock, provider, iid = assessed()
    old = result(monitor, iid)
    question = monitor.pending_questions()[0]
    with pytest.raises(ValueError, match='stale'):
        apply_decision(monitor, result(monitor, iid, boot_id='previous-boot'))
    clock.value += 1
    monitor.candidate(candidate(clock(), cid='new', change=True))
    assert monitor.incident(iid).question_id == question.question_id
    replay, = monitor.pending_questions()
    assert (replay.question_id, replay.evidence_revision, replay.reply) == (
        question.question_id, 1, question.reply)
    assert apply_decision(monitor, old)
    current = monitor.incident(iid)
    assert current.revision == 2 and current.state is not IncidentState.RESOLVED
    assert current.answer is current.situation_assessment is current.help_needed is None
    assert current.pending
    assert not monitor.pending_questions()
    assert not any(e.kind == 'incident_resolved' for e in monitor.drain_events())

    clock.value += 2
    monitor.ingest_rgb(frame(clock()))
    provider.reply = CloudFallReply(VideoAssessment.OBSERVED_FALL, '새 영상')
    assert asyncio.run(monitor.run_once())
    next_question, = monitor.pending_questions()
    assert next_question.question_id != question.question_id
    assert next_question.evidence_revision == 2
    assert not apply_decision(monitor, old)


@pytest.mark.parametrize('failed', [False, True])
def test_finished_question_releases_only_latest_assessed_revision(failed):
    monitor, clock, provider, iid = assessed()
    old = json.loads(result(monitor, iid))
    first, = monitor.pending_questions()
    for revision in (2, 3):
        clock.value += 3
        monitor.candidate(candidate(clock(), cid=f'new-{revision}', change=True))
        monitor.ingest_rgb(frame(clock()))
        provider.reply = CloudFallReply(VideoAssessment.OBSERVED_FALL, '새 영상')
        assert asyncio.run(monitor.run_once())
        replay, = monitor.pending_questions()
        assert (replay.question_id, replay.evidence_revision, replay.reply) == (
            first.question_id, 1, first.reply)
    monitor.drain_events()
    if failed:
        old['action'] = 'confirmation_failed'
        for key in ('subject_key', 'situation_assessment', 'help_needed'):
            old.pop(key)
    assert apply_decision(monitor, json.dumps(old))
    latest, = monitor.pending_questions()
    assert latest.evidence_revision == 3 and latest.question_id != first.question_id
    events = monitor.drain_events()
    completed = next(e for e in events if e.kind == (
        'agent_check_failed' if failed else 'voice_result'))
    assert completed.question_id == first.question_id and completed.evidence_revision == 1
    assert sum(e.kind == 'question_requested' for e in events) == 1
    assert not any(e.kind == 'incident_resolved' for e in events)


@pytest.mark.parametrize('help_needed', [False, True])
def test_original_answer_is_journaled_against_original_evidence(tmp_path, help_needed):
    from malbut_agent_server.adapters.outbound.sqlite_fall_journal import SqliteFallJournal

    monitor, clock, _, iid = assessed()
    journal = SqliteFallJournal(tmp_path / 'private' / 'fall.sqlite', device_id='robot')
    monitor._journal = journal
    try:
        old = result(monitor, iid, help_needed=help_needed)
        clock.value += 1
        monitor.candidate(candidate(clock(), cid='new', change=True))
        assert apply_decision(monitor, old)
        records = []
        while (row := journal.pending()) is not None:
            records.append(json.loads(row['payload']))
            journal.acknowledge(row['event_id'])
        completed = next(r for r in records if r['eventKind'] == (
            'confirmation_completed' if help_needed else 'voice_result'))
        assert completed['evidenceRevision'] == 1
        assert completed['answer'] == ('help_request' if help_needed else 'okay')
        latest, = journal.unresolved()
        assert latest['evidenceRevision'] == 2
        assert latest['answer'] == ('help_request' if help_needed else None)
        assert latest['state'] == ('help_required' if help_needed else 'verifying')
        assert monitor.incident(iid).pending
    finally:
        journal.close()


def test_newer_normal_video_cannot_clear_help_requested_in_original_session():
    monitor, clock, provider, iid = assessed()
    old = result(monitor, iid, help_needed=True)
    clock.value += 3
    monitor.candidate(candidate(clock(), cid='new', change=True))
    monitor.ingest_rgb(frame(clock()))
    provider.reply = CloudFallReply(VideoAssessment.NORMAL_ACTIVITY, '새 정상 영상')
    assert asyncio.run(monitor.run_once())
    monitor.drain_events()
    assert apply_decision(monitor, old)
    current = monitor.incident(iid)
    assert current.state is IncidentState.HELP_REQUIRED
    assert current.answer is VoiceAnswer.HELP and current.help_needed is True
    assert not monitor.pending_questions()
    assert not any(e.kind == 'analysis_completed' for e in monitor.drain_events())
    with pytest.raises(ValueError, match='clearance'):
        apply_decision(monitor, json.dumps(dict(
            action='dismiss_normal', boot_id=monitor.boot_id, incident_id=iid,
            evidence_revision=current.revision)))


@pytest.mark.parametrize('value', [VoiceAnswer.HELP, VoiceAnswer.OKAY, VoiceAnswer.FAILED])
def test_legacy_answer_releases_vlm_backed_question_snapshot(value):
    monitor, clock, _, iid = assessed()
    old = monitor.incident(iid).question_id
    assert answer(monitor, iid, value)
    assert not monitor.pending_questions()
    clock.value += 1
    monitor.candidate(candidate(clock(), cid='new', change=True))
    assert monitor.incident(iid).question_id is None
    assert not any(e.question_id == old for e in monitor.pending_questions())


@pytest.mark.parametrize('value', [VoiceAnswer.HELP, VoiceAnswer.OKAY, VoiceAnswer.FAILED])
def test_legacy_answer_can_finish_original_question_after_new_evidence(value):
    monitor, clock, provider, iid = assessed()
    first, = monitor.pending_questions()
    clock.value += 3
    monitor.candidate(candidate(clock(), cid='new', change=True))
    monitor.ingest_rgb(frame(clock()))
    provider.reply = CloudFallReply(VideoAssessment.OBSERVED_FALL, '새 영상')
    assert asyncio.run(monitor.run_once())
    monitor.drain_events()
    assert answer(monitor, iid, value, qid=first.question_id, revision=1)
    current = monitor.incident(iid)
    assert current.revision == 2 and current.state is not IncidentState.RESOLVED
    voice = next(e for e in monitor.drain_events() if e.kind == 'voice_result')
    assert voice.evidence_revision == 1
    if value is VoiceAnswer.HELP:
        assert current.state is IncidentState.HELP_REQUIRED
        assert not monitor.pending_questions()
    else:
        latest, = monitor.pending_questions()
        assert latest.question_id != first.question_id and latest.evidence_revision == 2
        assert current.answer is None


def test_late_vlm_recheck_cannot_overturn_users_resolved_result():
    async def scenario():
        monitor, clock, provider = make()
        enable(monitor)
        monitor.ingest_rgb(frame(clock()))
        iid = monitor.candidate(candidate())
        provider.reply = CloudFallReply(VideoAssessment.SUSPECTED_FALL, '관측')
        await monitor.run_once()
        clock.value += 3
        monitor.ingest_rgb(frame(clock()))
        monitor.request_recheck(iid)
        provider.started.clear()
        provider.release = asyncio.Event()
        task = asyncio.create_task(monitor.run_once())
        await provider.started.wait()
        assert apply_decision(monitor, result(monitor, iid))
        provider.reply = CloudFallReply(VideoAssessment.OBSERVED_FALL, '늦은 관측')
        provider.release.set()
        await task
        assert monitor.incident(iid).state is IncidentState.RESOLVED
        assert monitor.incident(iid).situation_assessment == 'resolved'
        assert not monitor.pending_questions()
    asyncio.run(scenario())
