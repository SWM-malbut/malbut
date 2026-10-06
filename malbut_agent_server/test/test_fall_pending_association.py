"""Association UNKNOWN is distinct from both MATCHED and a new scene case.

Synthetic observations/time and scripted Cloud replies only. No network/audio.
"""

import asyncio
from dataclasses import replace
import json

import pytest

from malbut_agent_server.adapters.outbound.sqlite_fall_journal import SqliteFallJournal
from malbut_agent_server.domain.fall_monitoring import (
    CandidateKind, IncidentState, SubjectFrame, VideoAssessment, VoiceAnswer,
)
from malbut_agent_server.fall_runtime import event_metadata
from malbut_agent_server.ports.fall_event_journal import FallJournalError
from malbut_fall_coordinator.fall_confirmation import FallConfirmationCoordinator
from test_cloud_fall_monitor import candidate, enable, frame, make
from test_fall_cloud_association import BOX, HELPER, finding, pose, reply


def feed(m, clock, stamp, people=(pose(),), *, stationary=True):
    clock.value = stamp
    m.ingest_rgb(frame(stamp))
    m.ingest_subject_frame(SubjectFrame(stamp, people, .5, stationary))


def advance(m, clock, end, people=(), *, stationary=True):
    while clock() < end:
        feed(m, clock, min(end, clock() + .2), people, stationary=stationary)


def open_case(*, outcome='help', journal=None):
    m, clock, provider = make(clip_window_s=5)
    m._journal = journal
    enable(m)
    feed(m, clock, 100)
    iid = m.candidate(candidate())
    provider.reply = reply(finding(assessment=VideoAssessment.OBSERVED_FALL))
    assert asyncio.run(m.run_once())
    q, = [e for e in m.drain_events() if e.kind == 'question_requested']
    if outcome != 'pending':
        assert m.confirmation_result(incident_id=iid, question_id=q.question_id,
            subject_key=q.subject_key, evidence_revision=q.evidence_revision,
            situation_assessment='confirmed_incident' if outcome == 'help' else 'resolved',
            help_needed=outcome == 'help')
    advance(m, clock, 105, (pose(),))  # Retain the last measured incident box.
    m.drain_events()
    provider.reply = reply(finding())
    return m, clock, provider, q


def pending_case(**kwargs):
    m, clock, provider, q = open_case(**kwargs)
    advance(m, clock, 160)  # Empty Pose frames; no fabricated tracking.
    assert asyncio.run(m.run_once())
    events = m.drain_events()
    d, = [e.discovery for e in events if e.discovery]
    assert d.association_review.status == 'pending'
    assert d.subject_key is None and d.incident_id is None and d.association_link is None
    assert d.association_review.candidate_incident_id == q.incident_id
    assert not any(e.kind in {'incident_opened', 'question_requested'} for e in events)
    return m, clock, provider, q, d


@pytest.mark.parametrize('outcome', ['help', 'pending'])
def test_missing_pose_keeps_original_case_and_records_unassociated_observation(outcome):
    m, clock, provider, q, d = pending_case(outcome=outcome)
    before = m.incident(q.incident_id)
    advance(m, clock, 179.9)
    assert not asyncio.run(m.run_once())
    assert len(m._incidents) == 1 and m.incident(q.incident_id) == before
    assert len(provider.calls) == 2
    assert d.reason == 'no_matching_track'
    assert d.association_review.deadline == 180
    assert m.incident(q.incident_id).answer is (VoiceAnswer.HELP if outcome == 'help' else None)


def test_timeout_creates_one_scene_verification_without_answer_transfer_or_cloud_call():
    m, clock, provider, q, d = pending_case()
    original = m.incident(q.incident_id)
    advance(m, clock, 180)
    assert not asyncio.run(m.run_once())  # Local transition, not a Cloud attempt.
    events = m.drain_events()
    new, = [e for e in events if e.kind == 'question_requested']
    assert new.incident_id != q.incident_id and new.confirmation_scope == 'scene'
    scene = m.incident(new.incident_id)
    assert scene.answer is None and scene.state is IncidentState.VERIFYING
    assert scene.last_observed_at == 160  # Original video time, not timeout time.
    assert m.incident(q.incident_id) == original
    assert len(provider.calls) == 2 and not m._association_wait.pending
    terminal, = [e.discovery for e in events if e.discovery]
    assert terminal.discovery_id == d.discovery_id
    assert terminal.reason == 'association_wait_expired'
    assert terminal.association_review.status == 'verification_required'
    assert terminal.subject_key is None and terminal.association_link is None
    # Subsequent detections stay in that one scene queue, not a per-minute question.
    for stamp in (220, 280, 340):
        advance(m, clock, stamp)
        assert asyncio.run(m.run_once())
        assert not any(e.kind == 'question_requested' for e in m.drain_events())
    assert len(m._incidents) == 2
    assert m.drain_clip_ranges() and m._people is not None


@pytest.mark.parametrize('change', ['motion', 'other_place', 'no_boxes', 'invalid_locations',
                                    'two_people', 'moving_camera', 'unknown_camera', 'old_anchor',
                                    'closed_case', 'competing_cases'])
def test_wait_never_hides_new_motion_other_people_or_unreliable_position(change):
    m, clock, provider, q = open_case(outcome='okay' if change == 'closed_case' else 'help')
    if change == 'competing_cases':
        feed(m, clock, 105.2, (pose(), pose('other')))
        other = m.candidate(candidate(clock(), subject='other', cid='other-candidate'))
        assert asyncio.run(m.run_once())
        advance(m, clock, 106, (pose(), pose('other')))
        assert m.incident(other).question_id
        m.drain_events()
    end = 200 if change == 'old_anchor' else 160
    advance(m, clock, end, stationary=change not in {'moving_camera', 'unknown_camera'})
    if change == 'motion':
        provider.reply = reply(finding(assessment=VideoAssessment.OBSERVED_FALL))
    elif change == 'other_place':
        provider.reply = reply(finding(HELPER))
    elif change == 'no_boxes':
        provider.reply = reply(replace(finding(), regions=()))
    elif change == 'invalid_locations':
        provider.reply = replace(reply(finding()), localization_failed=True)
    elif change == 'two_people':
        provider.reply = reply(finding(), finding(HELPER))
    assert asyncio.run(m.run_once())
    events = m.drain_events()
    new, = [e for e in events if e.kind == 'question_requested']
    assert new.incident_id != q.incident_id
    assert not m._association_wait.pending
    assert all(e.discovery.association_review is None for e in events if e.discovery)


def test_late_measured_pose_can_link_without_ever_creating_a_scene_or_question():
    m, clock, provider, q = open_case()
    advance(m, clock, 159.6, (pose(),))
    # Two new RGB timestamps, Pose callback delayed (NOT an explicit empty frame).
    m.buffer.clear()
    for stamp in (159.8, 160):
        clock.value = stamp
        m.ingest_rgb(frame(stamp))
    assert asyncio.run(m.run_once())
    d, = [e.discovery for e in m.drain_events() if e.discovery]
    assert d.association_review.status == 'pending'
    original = m.incident(q.incident_id)
    m.ingest_subject_frame(SubjectFrame(159.8, (pose(),), .5, True))
    m.ingest_subject_frame(SubjectFrame(160, (pose(),), .5, True))
    events = m.drain_events()
    linked, = [e.discovery for e in events if e.discovery]
    assert linked.association_review.status == 'matched'
    assert linked.incident_id == q.incident_id and linked.subject_key == q.subject_key
    assert linked.association_link is not None
    assert not m._association_wait.pending
    assert m.incident(q.incident_id) == original
    assert not any(e.kind in {'question_requested', 'incident_opened'} for e in events)
    clock.value = 180
    asyncio.run(m.run_once())
    assert len(m._incidents) == 1 and len(provider.calls) == 2


def test_reappearing_pose_with_new_continuity_token_is_not_identity_proof():
    m, clock, provider, q, _ = pending_case()
    advance(m, clock, 161, (pose(),))
    assert len(m._association_wait.pending) == 1
    assert not any(e.kind == 'cloud_discovery_linked' for e in m.drain_events())


@pytest.mark.parametrize('change', ['camera', 'consent', 'connection', 'runtime_block'])
def test_control_change_cancels_wait_without_replaying_questions(change):
    m, clock, provider, q, d = pending_case()
    if change == 'runtime_block':
        m.set_cloud_block('control_unavailable')
    else:
        m.configure(enabled=True, camera_enabled=change != 'camera',
                    cloud_consent=change != 'consent', connected=change != 'connection')
    events = m.drain_events()
    cancelled, = [e.discovery for e in events if e.discovery]
    assert cancelled.discovery_id == d.discovery_id
    assert cancelled.association_review.status == 'cancelled'
    assert not m._association_wait.pending
    assert not any(e.kind == 'question_requested' for e in events)
    clock.value = 185
    asyncio.run(m.run_once())
    assert len(m._incidents) == 1 and len(provider.calls) == 2
    assert m.incident(q.incident_id).state is IncidentState.HELP_REQUIRED


def test_repeated_findings_never_extend_the_first_deadline():
    m, clock, provider, q, first = pending_case()
    advance(m, clock, 170)
    req = provider.calls[-1]
    m._record_crosscheck(req, reply(finding()), ((),) * len(req.window.frames),
                        m._scene_incident_versions())
    ds = [e.discovery for e in m.drain_events() if e.discovery]
    assert len(ds) == 1 and ds[0].association_review.deadline == first.association_review.deadline
    clock.value = 180
    asyncio.run(m.run_once())
    assert len([e for e in m.drain_events() if e.kind == 'question_requested']) == 1
    assert not m._association_wait.pending and len(provider.calls) == 2


def test_candidate_closure_cannot_clear_the_unassociated_observation():
    m, clock, provider, q, _ = pending_case(outcome='pending')
    assert m.confirmation_result(incident_id=q.incident_id, question_id=q.question_id,
        subject_key=q.subject_key, evidence_revision=q.evidence_revision,
        situation_assessment='resolved', help_needed=False)
    asyncio.run(m.run_once())
    new, = [e for e in m.drain_events() if e.kind == 'question_requested']
    assert m.incident(q.incident_id).state is IncidentState.RESOLVED
    assert m.incident(new.incident_id).answer is None


def test_pending_and_timeout_are_durable_without_rgb_or_claimed_identity(tmp_path):
    journal = SqliteFallJournal(tmp_path / 'private/events.sqlite', device_id='robot')
    try:
        m, clock, provider, q, d = pending_case(journal=journal)
        stored = json.loads(journal._db.execute(
            'select payload from cloud_discoveries order by sequence desc limit 1').fetchone()[0])
        assert stored['association_review']['status'] == 'pending'
        assert stored['incident_id'] is None and stored['subject_key'] is None
        assert 'jpeg' not in json.dumps(stored) and 'private-model-text' not in json.dumps(stored)
        clock.value = 180
        asyncio.run(m.run_once())
        stored = json.loads(journal._db.execute(
            'select payload from cloud_discoveries order by sequence desc limit 1').fetchone()[0])
        assert stored['association_review']['status'] == 'verification_required'
        assert stored['discovery_id'] == d.discovery_id and stored['incident_id'] != q.incident_id
    finally:
        journal.close()


def test_discovery_journal_failure_disables_monitor_without_unpersisted_pending(tmp_path):
    journal = SqliteFallJournal(tmp_path / 'private/events.sqlite', device_id='robot')
    m, clock, provider, q = open_case(journal=journal)
    advance(m, clock, 160)
    journal.close()
    with pytest.raises(FallJournalError):
        asyncio.run(m.run_once())
    assert not m._enabled and not m._association_wait.pending
    assert not m.drain_events()


def test_manager_gets_no_new_confirmation_request_during_wait():
    m, clock, provider, q, _ = pending_case()
    coordinator = FallConfirmationCoordinator()
    for e in m.pending_questions():
        coordinator.receive(json.dumps(dict(event_metadata(e), boot_id=m.boot_id)))
    assert not coordinator.requests
    clock.value = 180
    asyncio.run(m.run_once())
    for e in m.drain_events():
        coordinator.receive(json.dumps(dict(event_metadata(e), boot_id=m.boot_id)))
    assert len(coordinator.requests) == 1


def test_deadline_tick_does_not_wait_for_another_cloud_request_to_finish():
    m, clock, provider, _, _ = pending_case()

    async def run():
        provider.started.clear()
        provider.release = asyncio.Event()
        clock.value = 161
        m.candidate(candidate(clock(), subject='different-person', cid='new-motion'))
        task = asyncio.create_task(m.run_once())
        await provider.started.wait()
        assert not task.done()
        clock.value = 180
        m.maintain_associations()
        qs = [e for e in m.drain_events() if e.kind == 'question_requested']
        assert len(qs) == 1 and qs[0].confirmation_scope == 'scene'
        assert not m._association_wait.pending and not task.done()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_metadata_queue_is_bounded_and_overflow_falls_back_to_verification():
    m, clock, provider, _, _ = pending_case()
    m._association_wait.max_entries = 1
    advance(m, clock, 170)
    req = provider.calls[-1]
    m._record_crosscheck(req, reply(finding()), ((),) * len(req.window.frames),
                        m._scene_incident_versions())
    assert len(m._association_wait.pending) == 1
    assert len([e for e in m.drain_events() if e.kind == 'question_requested']) == 1
    m.maintain_associations()
    assert not m._association_wait.pending
    assert not any(e.kind == 'question_requested' for e in m.drain_events())


def test_ten_minutes_without_pose_delays_but_does_not_claim_to_solve_identity_loss():
    m, clock, provider, q = open_case()
    events = []
    while clock() < 705:
        feed(m, clock, min(705, clock() + .2), ())
        asyncio.run(m.run_once())
        events.extend((clock(), e) for e in m.drain_events())
    opened = [(t, e) for t, e in events if e.kind == 'incident_opened']
    questions = [(t, e) for t, e in events if e.kind == 'question_requested']
    assert len(opened) == len(questions) == 1  # Plus the original A = 2 / 2.
    assert 180 <= questions[0][0] <= 180.4  # Not at the 160-second rediscovery.
    assert m.incident(q.incident_id).state is IncidentState.HELP_REQUIRED
    assert len(m._incidents) == 2 and len(provider.calls) == 11
    assert not m._association_wait.pending


@pytest.mark.parametrize('changes', [dict(status='matched_by_position'),
                                    dict(candidate_revision=True), dict(deadline=float('nan'))])
def test_review_metadata_rejects_invalid_states(changes):
    from malbut_agent_server.domain.fall_monitoring import CloudAssociationReview
    with pytest.raises(ValueError):
        CloudAssociationReview(**dict(dict(candidate_incident_id='test', candidate_revision=1,
                                          deadline=120), **changes))
