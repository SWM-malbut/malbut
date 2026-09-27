"""Cloud-only OR trigger, bounded scene questions and no guessed person identity."""

import asyncio
from dataclasses import replace
import json

import pytest

from malbut_agent_server.domain.fall_monitoring import (
    CloudFallReply, IncidentState, VideoAssessment, VoiceAnswer,
)
from malbut_agent_server.fall_runtime import apply_decision, event_metadata
from test_cloud_fall_monitor import candidate, enable, frame, make
from test_fall_cloud_association import finding, reply, feed, pose, HELPER


def scan(monitor, clock, provider, value, stamp=160):
    clock.value = stamp
    monitor.ingest_rgb(frame(stamp - .5))
    monitor.ingest_rgb(frame(stamp))
    provider.reply = value
    assert asyncio.run(monitor.run_once())
    return monitor.drain_events()


def setup(value=None):
    monitor, clock, provider = make()
    enable(monitor)
    events = scan(monitor, clock, provider, value or reply(finding()))
    question, = [e for e in events if e.kind == 'question_requested']
    return monitor, clock, provider, question, events


def confirm(monitor, question, **changes):
    return apply_decision(monitor, json.dumps(dict(
        action='confirmation_result', boot_id=monitor.boot_id,
        incident_id=question.incident_id, question_id=question.question_id,
        evidence_revision=question.evidence_revision, subject_key=None,
        situation_assessment='resolved', help_needed=False) | changes))


@pytest.mark.parametrize('value', [
    reply(finding()),
    reply(finding(assessment=VideoAssessment.OBSERVED_FALL)),
    CloudFallReply(VideoAssessment.SUSPECTED_FALL, 'no location'),
    CloudFallReply(VideoAssessment.SUSPECTED_FALL, 'bad location', localization_failed=True),
])
def test_no_pose_no_candidate_still_opens_and_questions_without_extra_call(value):
    monitor, _, provider, q, events = setup(value)
    incident = monitor.incident(q.incident_id)
    assert q.subject_key is None and q.confirmation_scope == 'scene'
    assert incident.state is IncidentState.VERIFYING
    assert incident.video.assessment is value.assessment and incident.auto_normal_blocked
    assert incident.attempts == 1 and incident.rechecks == 0 and not incident.pending
    assert event_metadata(q)['confirmation_scope'] == 'scene'
    assert monitor.pending_questions()[0].confirmation_scope == 'scene'
    assert len(provider.calls) == 1 and not asyncio.run(monitor.run_once())
    assert not any(e.kind == 'notification_requested' for e in events)


@pytest.mark.parametrize('value', [VideoAssessment.NORMAL_ACTIVITY, VideoAssessment.UNOBSERVABLE])
def test_no_positive_signal_does_not_invent_a_scene_incident(value):
    monitor, clock, provider = make()
    enable(monitor)
    events = scan(monitor, clock, provider, CloudFallReply(value, 'no positive finding'))
    assert not any(e.incident_id for e in events)
    assert not monitor.pending_questions()


def test_repeated_and_multiple_findings_keep_separate_records_but_one_general_question():
    monitor, clock, provider, q, events = setup(reply(finding(), finding(HELPER), finding()))
    discoveries = [e.discovery for e in events if e.discovery]
    assert len({d.discovery_id for d in discoveries}) == 3
    assert {d.incident_id for d in discoveries} == {q.incident_id}
    assert all(d.subject_key is None for d in discoveries)
    for stamp in (220, 280, 340):
        events = scan(monitor, clock, provider, reply(finding()), stamp)
        assert not any(e.kind in {'question_requested', 'incident_opened'} for e in events)
        assert next(e.discovery for e in events if e.discovery).incident_id == q.incident_id
    incident = monitor.incident(q.incident_id)
    assert incident.question_id == q.question_id and incident.revision == 1
    assert (incident.attempts, incident.rechecks) == (1, 0)
    assert len(provider.calls) == 4  # Only scheduled scans, no follow-up calls.


@pytest.mark.parametrize('assessment', ['resolved', 'unknown', 'confirmed_incident'])
def test_scene_no_help_reply_is_recorded_but_cannot_clear_unidentified_person(assessment):
    monitor, _, _, q, _ = setup()
    assert confirm(monitor, q, situation_assessment=assessment)
    incident = monitor.incident(q.incident_id)
    assert incident.state is IncidentState.RECHECK_REQUIRED
    assert incident.answer is VoiceAnswer.UNCLEAR
    assert incident.situation_assessment == assessment and incident.help_needed is False
    assert incident.close_reason is None and not monitor.pending_questions()
    events = monitor.drain_events()
    assert [e.kind for e in events] == ['voice_result', 'decision_required']
    assert confirm(monitor, q, situation_assessment=assessment)
    assert not monitor.drain_events()


def test_scene_help_request_enters_help_state_without_claiming_confirmed_fall():
    monitor, clock, provider, q, _ = setup()
    assert confirm(monitor, q, situation_assessment='unknown', help_needed=True)
    incident = monitor.incident(q.incident_id)
    assert incident.state is IncidentState.HELP_REQUIRED and not incident.fall_seen
    events = monitor.drain_events()
    assert sum(e.kind == 'notification_requested' for e in events) == 1
    events = scan(monitor, clock, provider,
                  reply(finding(assessment=VideoAssessment.OBSERVED_FALL)), 220)
    assert monitor.incident(q.incident_id).state is IncidentState.HELP_REQUIRED
    assert not any(e.kind in {'question_requested', 'notification_requested'} for e in events)


def test_scene_normal_scan_or_incident_recheck_cannot_clear_the_unidentified_case():
    monitor, clock, provider, q, _ = setup()
    confirm(monitor, q)
    events = scan(monitor, clock, provider,
                  CloudFallReply(VideoAssessment.NORMAL_ACTIVITY, 'helper moving normally'), 220)
    assert not any(e.kind == 'incident_resolved' for e in events)
    assert monitor.incident(q.incident_id).video.assessment is VideoAssessment.SUSPECTED_FALL
    assert not monitor.request_recheck(q.incident_id)
    assert monitor.drain_events()[0].reason == 'target_unidentified'
    assert len(provider.calls) == 2


def test_new_pose_person_is_not_guessed_to_be_the_unidentified_cloud_person():
    monitor, clock, provider, q, _ = setup()
    feed(monitor, clock, 161, (pose('helper', HELPER),))
    iid = monitor.candidate(candidate(161, subject='helper'))
    assert iid != q.incident_id
    assert monitor.incident(q.incident_id).subject_key is None
    before = monitor.incident(iid)
    assert confirm(monitor, q)
    assert monitor.incident(iid) == before
    assert not confirm(monitor, q, subject_key='helper')


def test_stronger_scene_evidence_revises_once_and_rejects_previous_answer():
    monitor, clock, provider, q, _ = setup()
    confirm(monitor, q)
    events = scan(monitor, clock, provider,
                  reply(finding(assessment=VideoAssessment.OBSERVED_FALL)), 220)
    new_q, = [e for e in events if e.kind == 'question_requested']
    assert new_q.incident_id == q.incident_id and new_q.evidence_revision == 2
    assert not confirm(monitor, q)
    events = scan(monitor, clock, provider,
                  reply(finding(assessment=VideoAssessment.OBSERVED_FALL)), 280)
    assert not any(e.kind == 'question_requested' for e in events)


def test_scene_closed_while_scan_pending_is_not_reopened_from_old_evidence():
    async def run():
        monitor, clock, provider = make()
        enable(monitor)
        clock.value = 160
        monitor.ingest_rgb(frame(160))
        provider.reply = reply(replace(finding(), regions=()))
        await monitor.run_once()
        q = next(e for e in monitor.drain_events() if e.kind == 'question_requested')
        clock.value = 220
        monitor.ingest_rgb(frame(220))
        provider.started.clear()
        provider.release = asyncio.Event()
        task = asyncio.create_task(monitor.run_once())
        await provider.started.wait()
        # Explicit operator closure, not a scene normal/okay auto-decision.
        monitor.resolve(q.incident_id, revision=1, reason='response_completed')
        monitor.drain_events()
        provider.release.set()
        await task
        events = monitor.drain_events()
        assert not any(e.kind in {'incident_opened', 'question_requested'} for e in events)
        assert next(e.discovery for e in events if e.discovery).reason == 'incident_changed_during_scan'
    asyncio.run(run())


@pytest.mark.parametrize('change', ['consent', 'camera', 'disabled', 'control'])
def test_permission_loss_while_scan_pending_cannot_start_a_scene_question(change):
    async def run():
        monitor, clock, provider = make()
        enable(monitor)
        clock.value = 160
        monitor.ingest_rgb(frame(160))
        provider.reply = reply(replace(finding(), regions=()))
        provider.release = asyncio.Event()
        task = asyncio.create_task(monitor.run_once())
        await provider.started.wait()
        if change == 'control':
            monitor.set_cloud_block('control_unavailable')
        else:
            monitor.configure(enabled=change != 'disabled', camera_enabled=change != 'camera',
                              cloud_consent=change != 'consent', connected=True)
        provider.release.set()
        await task
        assert not any(e.incident_id for e in monitor.drain_events())
        assert not monitor.pending_questions()
    asyncio.run(run())
