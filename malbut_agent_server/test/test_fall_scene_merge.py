"""Scene retirement is a lifecycle transition, never a normal/person answer."""

import asyncio
from concurrent.futures import Future
import json
import sys
from types import SimpleNamespace

import pytest

from malbut_agent_server.domain.fall_monitoring import CloudFallReply, IncidentState, VideoAssessment
from malbut_fall_coordinator.fall_confirmation import FallConfirmationCoordinator
from malbut_agent_server.fall_runtime import event_metadata
from test_cloud_fall_monitor import candidate
from test_fall_cloud_association import BOX, HELPER, feed, finding, pose
from test_fall_deferred_association import setup, start, link
from test_fall_coordinator_handoff import Handoff


def relay(coordinator, monitor, events):
    for event in events:
        assert coordinator.receive(json.dumps(dict(event_metadata(event),
            boot_id=monitor.boot_id, runtime_id='vlm'))) or event.kind not in {
                'question_requested', 'incident_merged'}


def test_lost_merge_event_is_replayed_and_old_question_cannot_resurrect():
    m, c, _, ds, initial = setup()
    coordinator = FallConfirmationCoordinator(runtime_id='vlm')
    relay(coordinator, m, initial)
    source = m.incident(ds[0].incident_id)
    iid, _ = link(m, c, start(m, ds[0]))
    events = m.drain_events()
    # Simulate loss of the terminal message, then the runtime heartbeat replay.
    relay(coordinator, m, (e for e in events if e.kind != 'incident_merged'))
    assert len(coordinator.requests) == 2
    relay(coordinator, m, m.pending_questions())
    relay(coordinator, m, initial)
    assert {r.incident_id for r in coordinator.requests.values()} == {iid}
    assert not m.confirmation_failed(incident_id=source.incident_id,
        question_id=source.question_id, evidence_revision=source.revision)
    assert not m.confirmation_result(incident_id=source.incident_id,
        question_id=source.question_id, subject_key=None, evidence_revision=source.revision,
        situation_assessment='resolved', help_needed=False)
    assert not m.drain_events()


def test_scene_retirement_preserves_target_question_original_evidence_after_update():
    m, c, provider, ds, initial = setup()
    coordinator = FallConfirmationCoordinator(runtime_id='vlm')
    relay(coordinator, m, initial)
    source = m.incident(ds[0].incident_id)
    sid = start(m, ds[0])
    feed(m, c, 160.25)
    iid = m.candidate(candidate(c()))
    provider.reply = CloudFallReply(VideoAssessment.SUSPECTED_FALL, 'target fixture')
    assert asyncio.run(m.run_once())
    relay(coordinator, m, m.drain_events())
    original = next(r for r in coordinator.requests.values() if r.incident_id == iid)
    m.ingest_discovery_track(sid, observed_at=c(), box=BOX)

    for t in (160.5, 160.75):
        feed(m, c, t)
        if t == 160.5:
            assert m.candidate(candidate(t, cid='new-evidence', change=True)) == iid
        m.ingest_discovery_track(sid, observed_at=t, box=BOX)
    relay(coordinator, m, m.drain_events())
    relay(coordinator, m, m.pending_questions())
    assert list(coordinator.requests.values()) == [original]
    assert m.incident(iid).revision == 2
    assert m.incident(source.incident_id).close_reason == 'findings_associated'
    replay = next(e for e in m.pending_questions() if e.incident_id == iid)
    assert replay.question_id == original.question_id and replay.evidence_revision == 1

    assert not m.confirmation_result(incident_id=source.incident_id,
        question_id=source.question_id, subject_key=None, evidence_revision=source.revision,
        situation_assessment='resolved', help_needed=False)
    assert m.confirmation_result(incident_id=iid, question_id=original.question_id,
        subject_key=original.subject_key, evidence_revision=original.revision,
        situation_assessment='resolved', help_needed=False)
    current = m.incident(iid)
    assert current.state is not IncidentState.RESOLVED
    assert current.answer is None and current.pending


def test_pruned_other_discovery_does_not_mean_whole_scene_associated():
    m, c, _, ds, _ = setup(findings=(finding(), finding(HELPER)))
    # Retry-cache eviction is NOT evidence that the other person disappeared.
    del m._discoveries[ds[1].discovery_id]
    iid, _ = link(m, c, start(m, ds[0]))
    scene = m.incident(ds[0].incident_id)
    assert scene.state is IncidentState.VERIFYING
    assert scene.unresolved_discovery_ids == (ds[1].discovery_id,)
    assert scene.associated_incident_ids == (iid,) and not scene.merged_into_incident_ids
    assert not any(e.kind == 'incident_merged' for e in m.drain_events())


def test_completeness_overflow_blocks_scene_retirement():
    m, c, _, ds, _ = setup()
    m._incidents[ds[0].incident_id].discovery_overflow = True
    link(m, c, start(m, ds[0]))
    assert m.incident(ds[0].incident_id).state is IncidentState.VERIFYING


def test_multiple_people_retire_scene_only_after_every_finding_is_linked():
    m, c, _, ds, _ = setup(findings=(finding(), finding(HELPER)))
    sessions = [start(m, d) for d in ds]
    targets = []
    for t in (160.25, 160.5, 160.75):
        feed(m, c, t, (pose(), pose('helper', HELPER)))
        if not targets:
            targets = [m.candidate(candidate(t, subject=key)) for key in ('person-1', 'helper')]
        for index, box in enumerate((BOX, HELPER)):
            m.ingest_discovery_track(sessions[index], observed_at=t, box=box)
            if index == 0:
                assert m.incident(ds[0].incident_id).state is not IncidentState.RESOLVED
    scene = m.incident(ds[0].incident_id)
    assert scene.state is IncidentState.RESOLVED
    assert set(scene.merged_into_incident_ids) == set(targets)
    assert all(m.incident(iid).answer is None for iid in targets)
    assert len([e for e in m.drain_events() if e.kind == 'incident_merged']) == 1


@pytest.mark.parametrize('phase', ['queued', 'accepted', 'awaiting_acceptance'])
def test_scene_merge_cancels_duplicate_managed_question_without_answer_transfer(monkeypatch, phase):
    monkeypatch.setitem(sys.modules, 'action_msgs.msg', SimpleNamespace(
        GoalStatus=SimpleNamespace(STATUS_SUCCEEDED=4, STATUS_ABORTED=6)))
    flow = Handoff()
    m, c, _, ds, initial = setup()
    flow.monitor, flow.clock = m, c
    if phase == 'queued':
        flow.link.manager_state = None
    flow.relay(initial)
    pending = flow.link.goal_future
    handle = result = None
    if phase == 'accepted':
        handle, result = flow.accept()
    iid, _ = link(m, c, start(m, ds[0]))
    flow.relay(m.drain_events())
    assert {r.incident_id for r in flow.link.coordinator.requests.values()} == {iid}
    assert flow.commands() == []
    if phase != 'queued':
        assert flow.link.request is None
        assert flow.link.client.send_goal_async.call_count == 1
        if phase == 'awaiting_acceptance':
            from unittest.mock import Mock
            handle, result = Mock(accepted=True), Future()
            handle.get_result_async.return_value = result
            handle.cancel_goal_async.return_value = Future()
            pending.set_result(handle)
        handle.cancel_goal_async.assert_called_once()
        # ACK alone isn't proof the original Agent stopped talking.
        handle.cancel_goal_async.return_value.set_result(SimpleNamespace(return_code=0))
        flow.link.tick()
        assert flow.link.client.send_goal_async.call_count == 1
        # A late success from the retired scene must not clear the person.
        flow.complete(result, assessment='resolved', help_needed=False)
        assert flow.commands() == []
        assert flow.link.request.incident_id == iid
        assert flow.link.client.send_goal_async.call_count == 2
    else:
        assert flow.link.client.send_goal_async.call_count == 0
        from test_fall_coordinator_handoff import manager_state
        flow.link.on_state(manager_state())
        assert flow.link.request.incident_id == iid
        assert flow.link.client.send_goal_async.call_count == 1
    assert m.incident(iid).answer is None
    assert m.incident(ds[0].incident_id).close_reason == 'findings_associated'
    flow.link.close()


@pytest.mark.parametrize('outcome', ['reject', 'cancel_transport_failed', 'result_transport_failed'])
def test_merge_waits_for_proven_mission_end_not_stale_idle_state(outcome):
    from unittest.mock import Mock
    from test_fall_coordinator_handoff import manager_state
    flow = Handoff()
    m, c, _, ds, initial = setup()
    flow.monitor, flow.clock = m, c
    flow.relay(initial)
    pending = flow.link.goal_future
    iid, _ = link(m, c, start(m, ds[0]))
    flow.relay(m.drain_events())
    if outcome == 'reject':
        pending.set_result(SimpleNamespace(accepted=False))
        assert flow.link.request.incident_id == iid
    else:
        handle, result = Mock(accepted=True), Future()
        handle.get_result_async.return_value = result
        if outcome == 'cancel_transport_failed':
            handle.cancel_goal_async.side_effect = RuntimeError('transport lost')
        pending.set_result(handle)
        if outcome == 'result_transport_failed':
            result.set_exception(RuntimeError('result transport lost'))
        flow.link.on_state(manager_state())
        assert flow.link.request is None
        assert flow.link.client.send_goal_async.call_count == 1
        assert {r.incident_id for r in flow.link.coordinator.requests.values()} == {iid}
    assert flow.commands() == []
    flow.link.close()
