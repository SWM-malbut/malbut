"""Later per-discovery attachment: actual core/SQLite, synthetic observations."""

import asyncio
from dataclasses import replace
import json

import pytest

from malbut_agent_server.adapters.outbound.sqlite_fall_journal import SqliteFallJournal
from malbut_agent_server.domain.fall_monitoring import (
    IncidentState, VideoAssessment, VoiceAnswer,
)
from malbut_agent_server.fall_runtime import event_metadata
from malbut_agent_server.ports.fall_event_journal import FallJournalError
from malbut_fall_coordinator.fall_confirmation import FallConfirmationCoordinator
from test_cloud_fall_monitor import answer, candidate, enable, make
from test_fall_cloud_association import BOX, HELPER, feed, finding, pose, reply


def setup(journal=None, findings=None, last_people=()):
    monitor, clock, provider = make()
    monitor._journal = journal
    enable(monitor)
    feed(monitor, clock, 159.5, ())
    feed(monitor, clock, 160, last_people)
    provider.reply = reply(*(findings or (finding(),)))
    assert asyncio.run(monitor.run_once())
    events = monitor.drain_events()
    discoveries = [e.discovery for e in events if e.discovery is not None]
    return monitor, clock, provider, discoveries, events


def start(monitor, discovery):
    sid = monitor.begin_discovery_tracking(discovery.discovery_id)
    assert monitor.begin_discovery_tracking(discovery.discovery_id) == sid
    for t in (159.5, 160):
        assert monitor.ingest_discovery_track(
            sid, observed_at=t, box=discovery.finding.regions[0].box).incident_id is None
    return sid


def link(monitor, clock, sid, *, existing=True, people=None):
    iid = None
    for t in (160.25, 160.5, 160.75):
        feed(monitor, clock, t, people if people is not None else (pose(),))
        if existing and iid is None:
            iid = monitor.candidate(candidate(t))
        result = monitor.ingest_discovery_track(sid, observed_at=t, box=BOX)
    return iid, result


def test_later_pose_attaches_to_same_incident_without_call_or_recheck():
    m, c, p, ds, initial = setup()
    source = m.incident(ds[0].incident_id)
    sid = start(m, ds[0])
    iid, result = link(m, c, sid)
    target = m.incident(iid)
    assert result.reason == 'matched_after_tracking' and result.incident_id == iid
    assert target.video.assessment is VideoAssessment.SUSPECTED_FALL
    assert target.candidate_sources == ('yolo_pose', 'cloud_crosscheck')
    assert target.attempts == target.rechecks == 0
    assert len(p.calls) == 1  # Completed crosscheck reused, not sent again.
    merged = m.incident(source.incident_id)
    assert merged.state is IncidentState.RESOLVED
    assert merged.close_reason == 'findings_associated'
    assert merged.merged_into_incident_ids == (iid,)
    assert merged.video == source.video and merged.answer == source.answer
    events = m.drain_events()
    linked = next(e.discovery for e in events if e.kind == 'cloud_discovery_linked')
    assert linked.discovery_id == ds[0].discovery_id
    assert linked.incident_id == iid and linked.subject_key == 'person-1'
    assert linked.association_link.source_incident_id == source.incident_id
    assert linked.association_link.pose_samples == 3
    assert linked.association_link.visual_samples == 5
    assert target.auto_normal_blocked and not target.normal_checks
    assert m.ingest_discovery_track(sid, observed_at=c(), box=BOX).reason == 'already_linked'
    assert not m.drain_events()
    with pytest.raises(ValueError, match='already associated'):
        m.begin_discovery_tracking(ds[0].discovery_id)


def test_creates_person_case_if_pose_has_no_candidate():
    m, c, p, ds, _ = setup()
    _, result = link(m, c, start(m, ds[0]), existing=False)
    target = m.incident(result.incident_id)
    assert target.subject_key == 'person-1'
    assert target.attempts == 1 and target.rechecks == 0 and not target.pending
    assert target.question_id and len(p.calls) == 1


def test_preserves_target_question_help_and_retry_budget():
    m, c, p, ds, _ = setup()
    sid = start(m, ds[0])
    feed(m, c, 160.25)
    iid = m.candidate(candidate(c()))
    answer(m, iid, VoiceAnswer.HELP)
    before = m.incident(iid)
    m._incidents[iid].rechecks = 2
    for t in (160.25, 160.5, 160.75):
        if t != 160.25:
            feed(m, c, t)
        result = m.ingest_discovery_track(sid, observed_at=t, box=BOX)
    after = m.incident(iid)
    assert result.incident_id == iid and after.question_id == before.question_id
    assert after.answer is VoiceAnswer.HELP and after.state is IncidentState.HELP_REQUIRED
    assert after.notification_level == before.notification_level and after.rechecks == 2
    assert after.revision == before.revision


def test_pending_question_keeps_id_and_old_negative_answer_is_invalidated():
    for old_answer in (None, VoiceAnswer.OKAY):
        m, c, _, ds, _ = setup()
        sid = start(m, ds[0])
        feed(m, c, 160.25)
        iid = m.candidate(candidate(c()))
        qid = m.ask_question(iid)
        if old_answer:
            answer(m, iid, old_answer)
        for t in (160.25, 160.5, 160.75):
            if t != 160.25:
                feed(m, c, t)
            result = m.ingest_discovery_track(sid, observed_at=t, box=BOX)
        target = m.incident(iid)
        assert result.incident_id == iid and target.answer is None
        if old_answer is None:
            assert target.question_id == qid and target.revision == 1
        else:
            assert target.question_id != qid and target.revision == 2
            assert not m.confirmation_result(
                incident_id=iid, question_id=qid, subject_key='person-1', evidence_revision=1,
                situation_assessment='resolved', help_needed=False)


def test_does_not_transfer_scene_answer_or_close_other_discovery():
    m, c, p, ds, initial = setup(findings=(finding(), finding(HELPER)))
    source_id = ds[0].incident_id
    assert source_id == ds[1].incident_id
    source = m.incident(source_id)
    assert m.confirmation_result(
        incident_id=source_id, question_id=source.question_id, subject_key=None,
        evidence_revision=source.revision, situation_assessment='resolved', help_needed=False)
    before = m.incident(source_id)
    iid, result = link(m, c, start(m, ds[0]))
    assert result.incident_id == iid
    assert m.incident(iid).answer is None
    after = m.incident(source_id)
    assert after == replace(before, unresolved_discovery_ids=(ds[1].discovery_id,),
                            associated_incident_ids=(iid,))
    assert after.state is IncidentState.RECHECK_REQUIRED
    assert m._discoveries[ds[1].discovery_id].discovery == ds[1]
    with pytest.raises(ValueError, match='normal closure'):
        m._incidents[iid].pending = False
        m.resolve(iid, revision=m.incident(iid).revision, reason='normal_verified')


def test_scene_help_remains_urgent_and_is_not_attributed_to_target():
    m, c, p, ds, _ = setup()
    scene = m.incident(ds[0].incident_id)
    m.confirmation_result(
        incident_id=scene.incident_id, question_id=scene.question_id, subject_key=None,
        evidence_revision=scene.revision, situation_assessment='confirmed_incident', help_needed=True)
    before = m.incident(ds[0].incident_id)
    iid, result = link(m, c, start(m, ds[0]))
    assert result.incident_id == iid
    assert m.incident(ds[0].incident_id) == replace(
        before, unresolved_discovery_ids=(), associated_incident_ids=(iid,))
    assert before.state is IncidentState.HELP_REQUIRED
    assert m.incident(iid).answer is None


@pytest.mark.parametrize('people,reason', [
    ((), 'pose_evidence_missing'),
    ((pose(box=HELPER),), 'no_matching_track'),
    ((pose(usable=False),), 'track_unusable'),
    ((pose(), pose('helper', BOX)), 'ambiguous_tracks'),
    ((pose(), pose('weak', BOX, False)), 'ambiguous_tracks'),
])
def test_cannot_select_missing_weak_or_competing_person(people, reason):
    m, c, _, ds, _ = setup()
    _, result = link(m, c, start(m, ds[0]), existing=False, people=people)
    assert result.reason == reason and result.incident_id is None
    assert not any(e.kind == 'cloud_discovery_linked' for e in m.drain_events())


@pytest.mark.parametrize('t,box', [(160.75, BOX), (160.25, None), (160.25, HELPER)])
def test_gap_missing_mask_or_jump_cannot_reacquire(t, box):
    m, c, _, ds, _ = setup()
    sid = start(m, ds[0])
    feed(m, c, t)
    assert m.ingest_discovery_track(sid, observed_at=t, box=box).reason == 'visual_track_broken'
    feed(m, c, t + .25)
    assert m.ingest_discovery_track(sid, observed_at=t + .25, box=BOX).reason == 'visual_track_broken'
    with pytest.raises(ValueError, match='visual_track_broken'):
        m.begin_discovery_tracking(ds[0].discovery_id)


def test_pose_gap_restarts_confirmation_and_reused_id_does_not_join_old_case():
    m, c, _, ds, _ = setup()
    sid = start(m, ds[0])
    feed(m, c, 160.25)
    iid = m.candidate(candidate(c()))
    old_token = m.incident(iid).subject_association_token
    m.ingest_discovery_track(sid, observed_at=c(), box=BOX)
    feed(m, c, 160.5, ())
    m.ingest_discovery_track(sid, observed_at=c(), box=BOX)
    for t in (160.75, 161, 161.25):
        feed(m, c, t)
        result = m.ingest_discovery_track(sid, observed_at=t, box=BOX)
    assert m._subject_evidence.latest('person-1')[1] != old_token
    assert result.reason == 'incident_target_continuity_unverified'


@pytest.mark.parametrize('action', ['camera_off', 'consent', 'control', 'pose_invalid'])
def test_old_session_cannot_apply_after_runtime_change(action):
    m, c, _, ds, _ = setup()
    sid = start(m, ds[0])
    if action == 'camera_off':
        m.configure(enabled=True, camera_enabled=False, cloud_consent=True, connected=True)
        enable(m)
    elif action == 'consent':
        enable(m, consent=False)
    elif action == 'control':
        m.set_cloud_block('control_unavailable')
    else:
        m.invalidate_subject_input()
    _, result = link(m, c, sid, existing=False)
    assert result.incident_id is None


def test_closed_source_is_not_resurrected():
    m, c, _, ds, _ = setup()
    sid = start(m, ds[0])
    source = m.incident(ds[0].incident_id)
    m.resolve(source.incident_id, revision=source.revision, reason='response_completed')
    _, result = link(m, c, sid)
    assert result.reason == 'source_incident_changed'


def test_two_discoveries_from_same_reply_cannot_claim_one_pose():
    m, c, _, ds, _ = setup(findings=(finding(), finding()))
    sids = [start(m, d) for d in ds]
    results = []
    for t in (160.25, 160.5, 160.75):
        feed(m, c, t)
        results = [m.ingest_discovery_track(s, observed_at=t, box=BOX) for s in sids]
    assert results[0].reason == 'matched_after_tracking'
    assert results[1].reason == 'target_claimed_by_other_finding'


def test_unknown_expired_and_wrong_sample_sessions_are_rejected():
    m, c, _, ds, _ = setup()
    assert m.ingest_discovery_track('unknown', observed_at=160, box=BOX).incident_id is None
    sid = m.begin_discovery_tracking(ds[0].discovery_id)
    with pytest.raises(ValueError, match='Cloud seed'):
        m.ingest_discovery_track(sid, observed_at=160, box=BOX)
    c.value = 221
    assert m.ingest_discovery_track(sid, observed_at=c(), box=BOX).reason == 'unknown_tracking_session'


def test_persists_original_and_link_without_rgb_and_reopens(tmp_path):
    path = tmp_path / 'private' / 'fall.sqlite'
    journal = SqliteFallJournal(path, device_id='robot')
    m, c, _, ds, initial = setup(journal)
    iid, result = link(m, c, start(m, ds[0]))
    assert result.incident_id == iid
    rows = journal.discoveries()
    assert len(rows) == 2
    assert rows[0]['association_link'] is None
    assert rows[1]['discovery_id'] == rows[0]['discovery_id']
    assert rows[1]['incident_id'] == iid
    assert rows[1]['association_link']['source_incident_id'] == ds[0].incident_id
    assert 'jpeg' not in json.dumps(rows) and 'private-model-text' not in json.dumps(rows)
    journal.close()
    reopened = SqliteFallJournal(path, device_id='robot')
    assert reopened.discoveries() == rows
    assert len(reopened.unresolved()) == 1  # Source is merged, never labeled normal.
    merged = json.loads(reopened._db.execute(
        'SELECT payload FROM incident_events WHERE incident_id=? ORDER BY sequence DESC LIMIT 1',
        (ds[0].incident_id,)).fetchone()[0])
    assert merged['eventKind'] == 'incident_merged'
    assert merged['assessment'] == 'suspected_fall'
    assert merged['mergedIntoIncidentIds'] == [iid]
    reopened.close()


def test_sqlite_failure_rolls_back_target_and_link_before_exposing_events(tmp_path):
    journal = SqliteFallJournal(tmp_path / 'private' / 'fall.sqlite', device_id='robot')
    m, c, _, ds, _ = setup(journal)
    sid = start(m, ds[0])
    before = journal.unresolved()
    original = journal._append_discovery

    def fail(**kwargs):
        original(**kwargs)
        raise OSError('simulated full disk')

    journal._append_discovery = fail
    with pytest.raises(FallJournalError, match='association persistence'):
        link(m, c, sid, existing=False)
    assert journal.unresolved() == before
    assert len(journal.discoveries()) == 1
    assert not any(e.kind == 'cloud_discovery_linked' for e in m.drain_events())
    assert len(m._incidents) == 1
    with pytest.raises(FallJournalError):
        enable(m)
    journal.close()


def test_manager_retires_scene_question_and_routes_only_target_question():
    m, c, _, ds, initial = setup()
    iid, result = link(m, c, start(m, ds[0]))
    coordinator = FallConfirmationCoordinator(runtime_id='run')
    for event in (*initial, *m.drain_events()):
        coordinator.receive(json.dumps(dict(event_metadata(event), boot_id='boot-1', runtime_id='run')))
    requests = tuple(coordinator.requests.values())
    assert len(requests) == 1
    assert {r.incident_id for r in requests} == {iid}
    assert {r.subject_key for r in requests} == {'person-1'}


def test_newer_pose_evidence_keeps_its_first_analysis_queued():
    # Candidate at 160.25 is AFTER the cached window end=160. It still needs
    # its newer video; attaching older positive evidence cannot cancel it.
    m, c, _, ds, _ = setup()
    iid, result = link(m, c, start(m, ds[0]))
    assert result.incident_id == iid
    assert m.incident(iid).pending and m.incident(iid).attempts == 0


def test_completed_crosscheck_replaces_covered_first_analysis_without_second_call():
    m, c, p, ds, _ = setup(last_people=(pose(),))
    iid = m.candidate(candidate(160))
    sid = start(m, ds[0])
    for t in (160.25, 160.5):
        feed(m, c, t)
        result = m.ingest_discovery_track(sid, observed_at=t, box=BOX)
    target = m.incident(iid)
    assert result.incident_id == iid
    assert target.attempts == 1 and target.rechecks == 0 and not target.pending
    assert not asyncio.run(m.run_once()) and len(p.calls) == 1


def test_closed_target_and_running_target_analysis_are_not_overwritten():
    for state in ('closed', 'running'):
        m, c, _, ds, _ = setup()
        sid = start(m, ds[0])
        feed(m, c, 160.25)
        iid = m.candidate(candidate(c()))
        if state == 'closed':
            m._incidents[iid].pending = False
            m.resolve(iid, revision=1, reason='response_completed')
        else:
            m._active_incident = iid
        before = m.incident(iid)
        for t in (160.25, 160.5, 160.75):
            if t != 160.25:
                feed(m, c, t)
            result = m.ingest_discovery_track(sid, observed_at=t, box=BOX)
        assert result.reason == ('target_incident_closed' if state == 'closed' else 'target_analysis_in_flight')
        assert m.incident(iid) == before


def test_capacity_and_missing_cloud_location_do_not_forge_link():
    m, c, _, ds, _ = setup()
    sid = start(m, ds[0])
    m.policy = replace(m.policy, max_incidents=1)
    _, result = link(m, c, sid, existing=False)
    assert result.reason == 'incident_capacity' and len(m._incidents) == 1
    m, c, _, ds, _ = setup(findings=(replace(finding(), regions=()),))
    with pytest.raises(ValueError, match='usable seed'):
        m.begin_discovery_tracking(ds[0].discovery_id)


def test_stale_target_and_mismatched_rgb_time_do_not_link():
    m, c, _, ds, _ = setup()
    sid = start(m, ds[0])
    for t in (160.25, 160.5, 160.75):
        feed(m, c, t)
    c.value = 164
    for t in (160.25, 160.5, 160.75):
        result = m.ingest_discovery_track(sid, observed_at=t, box=BOX)
    assert result.reason == 'current_target_unavailable'
    m, c, _, ds, _ = setup()
    sid = start(m, ds[0])
    c.value = 160.3
    assert m.ingest_discovery_track(sid, observed_at=c(), box=BOX).reason == 'rgb_sample_unavailable'
