"""Repeated low posture is an observation, not a fresh confirmation dialogue.

No model/network/ROS/audio calls. Synthetic measured Pose and Cloud replies
exercise the production monitor, identity proof and coordinator boundaries.
"""

import asyncio
from dataclasses import replace
import json

import pytest

from malbut_agent_server.domain.fall_monitoring import (
    IncidentState, SubjectCheckState, VideoAssessment, VoiceAnswer,
)
from malbut_agent_server.fall_runtime import apply_decision, event_metadata
from malbut_fall_coordinator.fall_confirmation import FallConfirmationCoordinator
from test_cloud_fall_monitor import candidate, enable, make
from test_fall_cloud_association import feed, finding, pose, reply, HELPER


def low(key='person-1', **kwargs):
    return replace(pose(key, **kwargs), state=SubjectCheckState.SUSPECTED)


def advance(monitor, clock, end, people=(low(),)):
    while clock() < end:
        feed(monitor, clock, min(end, clock() + .2), people)


def closed_case(journal=None):
    monitor, clock, provider = make(clip_window_s=5)
    monitor._journal = journal
    enable(monitor)
    advance(monitor, clock, 160)
    provider.reply = reply(finding())
    assert asyncio.run(monitor.run_once())
    q, = [e for e in monitor.drain_events() if e.kind == 'question_requested']
    coordinator = FallConfirmationCoordinator()
    assert coordinator.receive(json.dumps(dict(event_metadata(q), boot_id=monitor.boot_id)))
    request, = coordinator.requests.values()
    assert coordinator.complete(request, situation_assessment='resolved', help_needed=False)
    command, = coordinator.drain_commands()
    assert apply_decision(monitor, json.dumps(command))
    assert monitor.incident(q.incident_id).state is IncidentState.RESOLVED
    monitor.drain_events()
    return monitor, clock, provider, coordinator, q


def test_ten_minute_continuous_low_pose_keeps_one_closed_case_and_one_dialogue():
    m, clock, provider, coordinator, q = closed_case()
    original = m.incident(q.incident_id)
    for minute in range(1, 11):
        advance(m, clock, 160 + minute * 60)
        assert asyncio.run(m.run_once())
        events = m.drain_events()
        discovery, = [e.discovery for e in events if e.discovery]
        assert discovery.incident_id == q.incident_id
        assert discovery.reason == 'settled_episode_continues'
        for event in events:
            coordinator.receive(json.dumps(dict(event_metadata(event), boot_id=m.boot_id)))
        assert not any(e.kind in {'incident_opened', 'question_requested'} for e in events)
        assert not coordinator.requests and not m.pending_questions()
        assert m.incident(q.incident_id) == original
    assert len(provider.calls) == 11
    assert len(m._incidents) == 1 and len(m._settled_subjects) == 1


@pytest.mark.parametrize('change', ['upright', 'gap', 'weak', 'missing', 'invalid', 'camera', 'unknown'])
def test_episode_guard_does_not_survive_recovery_or_lost_identity(change):
    m, clock, provider, _, q = closed_case()
    if change == 'upright':
        advance(m, clock, 163, (replace(low(), state=SubjectCheckState.CLEAR),))
    elif change == 'gap':
        feed(m, clock, 163, (low(),))
    elif change == 'weak':
        advance(m, clock, 161, (low(usable=False),))
    elif change == 'missing':
        advance(m, clock, 161, ())
    elif change == 'invalid':
        m.invalidate_subject_input()
    elif change == 'camera':
        m.configure(enabled=True, camera_enabled=False, cloud_consent=True, connected=True)
        enable(m)
    people = (replace(low(), state=SubjectCheckState.UNKNOWN),) if change == 'unknown' else (low(),)
    advance(m, clock, 220, people)
    assert asyncio.run(m.run_once())
    events = m.drain_events()
    fresh, = [e for e in events if e.kind == 'question_requested']
    assert fresh.incident_id != q.incident_id
    assert m.incident(q.incident_id).state is IncidentState.RESOLVED


def test_another_person_is_not_hidden_by_a_cleared_still_lying_person():
    m, clock, provider, _, q = closed_case()
    advance(m, clock, 220, (low(), low('other', box=HELPER)))
    provider.reply = reply(finding(), finding(HELPER))
    assert asyncio.run(m.run_once())
    events = m.drain_events()
    fresh, = [e for e in events if e.kind == 'question_requested']
    assert fresh.subject_key == 'other' and fresh.incident_id != q.incident_id
    ds = [e.discovery for e in events if e.discovery]
    assert {d.subject_key for d in ds} == {'person-1', 'other'}
    assert len(m._incidents) == 2


def test_new_observed_motion_is_not_suppressed_by_prior_resting_answer():
    m, clock, provider, _, q = closed_case()
    advance(m, clock, 220)
    provider.reply = reply(finding(assessment=VideoAssessment.OBSERVED_FALL))
    assert asyncio.run(m.run_once())
    events = m.drain_events()
    fresh, = [e for e in events if e.kind == 'question_requested']
    assert fresh.incident_id != q.incident_id
    assert m.incident(fresh.incident_id).fall_seen


def test_new_pose_fall_is_not_cleared_by_prior_resting_answer():
    m, clock, provider, _, q = closed_case()
    advance(m, clock, 161)
    iid = m.candidate(candidate(clock(), cid='new-motion'))
    assert iid != q.incident_id
    assert asyncio.run(m.run_once())
    events = m.drain_events()
    fresh, = [e for e in events if e.kind == 'question_requested']
    assert fresh.incident_id == iid
    assert m.incident(iid).answer is None


def test_followup_is_journaled_as_discovery_not_invalid_closed_incident_event(tmp_path):
    from malbut_agent_server.adapters.outbound.sqlite_fall_journal import SqliteFallJournal
    journal = SqliteFallJournal(tmp_path / 'private/events.sqlite', device_id='robot')
    try:
        m, clock, provider, _, q = closed_case(journal)
        before = journal._db.execute('select count(*) from incident_events').fetchone()[0]
        advance(m, clock, 220)
        assert asyncio.run(m.run_once())
        assert journal._db.execute('select count(*) from incident_events').fetchone()[0] == before
        row = journal._db.execute('select payload from cloud_discoveries order by sequence desc limit 1').fetchone()
        data = json.loads(row[0])
        assert data['incident_id'] == q.incident_id
        assert data['reason'] == 'settled_episode_continues'
        assert data['assessment'] == 'suspected_fall'
        assert m.incident(q.incident_id).answer is VoiceAnswer.OKAY
        assert m.drain_clip_ranges()  # Preserve the actual recording linkage.
    finally:
        journal.close()


@pytest.mark.parametrize('outcome', ['pending', 'failed', 'okay', 'help'])
def test_scene_label_strengthening_never_restarts_same_confirmation(outcome):
    from test_fall_unidentified_verification import setup, scan, confirm
    m, clock, provider, q, _ = setup()
    if outcome == 'failed':
        assert m.confirmation_failed(incident_id=q.incident_id, question_id=q.question_id,
                                     evidence_revision=q.evidence_revision)
    elif outcome in {'okay', 'help'}:
        assert confirm(m, q, help_needed=outcome == 'help')
    previous = m.incident(q.incident_id)
    m.drain_events()
    for stamp in (220, 280, 340):
        events = scan(m, clock, provider,
                      reply(finding(assessment=VideoAssessment.OBSERVED_FALL)), stamp)
        assert not any(e.kind in {'question_requested', 'incident_opened'} for e in events)
        current = m.incident(q.incident_id)
        assert current.question_id == q.question_id and current.revision == previous.revision
        assert current.answer == previous.answer and current.fall_seen
    if outcome == 'failed':
        # Explicit retry stays available; this change only removes automatic
        # question restarts caused by periodic classification changes.
        assert m.ask_question(q.incident_id) != q.question_id
