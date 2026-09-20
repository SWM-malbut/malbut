"""Synthetic contracts only: no Cloud calls, robot motion or spoken questions."""

import asyncio
from dataclasses import replace
import json

import pytest

from malbut_agent_server.adapters.outbound.ollama_cloud_fall import build_payload, parse_reply
from malbut_agent_server.adapters.outbound.sqlite_fall_journal import SqliteFallJournal
from malbut_agent_server.application.fall_cloud_association import associate_finding
from malbut_agent_server.application.fall_subject_evidence import FallSubjectEvidence
from malbut_agent_server.domain.fall_monitoring import (
    CandidateKind, CloudFallReply, CloudPersonFinding, CloudPersonRegion, FrameWindow,
    SubjectCheckState, SubjectFrame, SubjectPose, VideoAssessment, VoiceAnswer,
)
from malbut_agent_server.fall_runtime import event_metadata
from malbut_agent_server.ports.cloud_fall import CloudFallProviderError
from malbut_agent_server.ports.fall_event_journal import FallJournalError
from test_cloud_fall_monitor import answer, candidate, enable, frame, make
from test_ollama_cloud_fall import request, response


BOX = (0.1, 0.4, 0.7, 0.9)
HELPER = (0.75, 0.1, 0.95, 0.95)


def pose(key='person-1', box=BOX, usable=True):
    return SubjectPose(key, box, SubjectCheckState.UNKNOWN, usable)


def finding(box=BOX, assessment=VideoAssessment.SUSPECTED_FALL):
    return CloudPersonFinding(assessment, CandidateKind.MOTION_SEEN
                              if assessment is VideoAssessment.OBSERVED_FALL
                              else CandidateKind.ALREADY_DOWN,
                              (CloudPersonRegion(0, box), CloudPersonRegion(1, box)))


def reply(*findings):
    return CloudFallReply(VideoAssessment.OBSERVED_FALL if any(
        f.assessment is VideoAssessment.OBSERVED_FALL for f in findings)
        else VideoAssessment.SUSPECTED_FALL, 'private-model-text', tuple(findings))


def feed(monitor, clock, stamp, people=(pose(),)):
    clock.value = stamp
    monitor.ingest_rgb(frame(stamp))
    monitor.ingest_subject_frame(SubjectFrame(stamp, people, 0.5))


def scene_setup():
    monitor, clock, provider = make()
    enable(monitor)
    feed(monitor, clock, 159.5)
    feed(monitor, clock, 160)
    provider.reply = reply(finding())
    return monitor, clock, provider


def discoveries(monitor):
    return [e.discovery for e in monitor.drain_events() if e.discovery is not None]


def test_snapshot_is_immutable_and_requires_two_exact_consistent_samples():
    bank = FallSubjectEvidence(retention_s=30, max_frames=10)
    bank.append(SubjectFrame(10, (pose(), pose('helper', HELPER)), 0.5))
    bank.append(SubjectFrame(10.5, (pose(), pose('helper', HELPER)), 0.5))
    snapshot = bank.snapshot(FrameWindow((frame(10), frame(10.5)), 10, 10.5, False))
    bank.clear()
    result = associate_finding(finding(), snapshot)
    assert result.subject_key == 'person-1' and result.reason == 'matched'
    assert associate_finding(replace(finding(), regions=(finding().regions[0],)),
                             snapshot).reason == 'insufficient_locations'
    assert associate_finding(finding(), (snapshot[0], ())).reason == 'no_matching_track'


@pytest.mark.parametrize('people,expected', [
    ((), 'no_matching_track'),
    ((pose(usable=False),), 'track_unusable'),
    ((pose(), pose('other')), 'ambiguous_tracks'),
    ((pose(), pose('weak-overlap', usable=False)), 'ambiguous_tracks'),
])
def test_weak_missing_and_ambiguous_tracks_are_not_assigned(people, expected):
    bank = FallSubjectEvidence(retention_s=30, max_frames=10)
    for t in (10, 10.5):
        bank.append(SubjectFrame(t, people, 0.5))
    snapshot = bank.snapshot(FrameWindow((frame(10), frame(10.5)), 10, 10.5, False))
    assert associate_finding(finding(), snapshot).reason == expected


def test_cloud_cannot_join_different_people_across_frames():
    bank = FallSubjectEvidence(retention_s=30, max_frames=10)
    bank.append(SubjectFrame(10, (pose('one'),), 0.5))
    bank.append(SubjectFrame(10.5, (pose('two'),), 0.5))
    snapshot = bank.snapshot(FrameWindow((frame(10), frame(10.5)), 10, 10.5, False))
    assert associate_finding(finding(), snapshot).reason == 'track_changed'


def cross_request():
    return replace(request(), purpose='crosscheck', incident_id=None, subject_key=None,
                   evidence_revision=0, sensors=None)


def raw_scene():
    return dict(assessment='suspected_fall', explanation='바닥에 있는 사람', findings=[dict(
        assessment='suspected_fall', kind='already_down', regions=[
            dict(frame_index=0, box=list(BOX)), dict(frame_index=1, box=list(BOX))])])


def test_crosscheck_wire_is_localized_without_sending_private_ids():
    value = raw_scene()
    parsed = parse_reply(response(json.dumps(value)), cross_request())
    assert parsed.findings == (finding(),)
    payload = build_payload(cross_request(), model='gemma4:31b')
    assert b'private-' not in payload
    assert 'frame_index' in json.loads(payload)['messages'][0]['content']
    with pytest.raises(CloudFallProviderError):
        parse_reply(response(json.dumps(value)), request())  # Incident contract unchanged.


@pytest.mark.parametrize('bad_region', [
    {'frame_index': 2, 'box': list(BOX)}, {'frame_index': True, 'box': list(BOX)},
    {'frame_index': 0, 'box': [-1, 0, 1, 1]}, {'frame_index': 0, 'box': [0, 0, 0, 1]},
    {'frame_index': 0, 'box': 'bad'}, {'frame_index': 0, 'box': list(BOX), 'person_id': 'fake'},
])
def test_bad_location_does_not_discard_a_valid_positive_scene(bad_region):
    value = raw_scene()
    value['findings'][0]['regions'][0] = bad_region
    parsed = parse_reply(response(json.dumps(value)), cross_request())
    assert parsed.assessment is VideoAssessment.SUSPECTED_FALL
    assert parsed.findings == () and parsed.localization_failed


def test_normal_scene_with_positive_findings_is_not_accepted_as_normal():
    value = raw_scene()
    value['assessment'] = 'normal_activity'
    with pytest.raises(CloudFallProviderError):
        parse_reply(response(json.dumps(value)), cross_request())


def test_missing_findings_legacy_response_remains_unidentified():
    monitor, _, provider = scene_setup()
    provider.reply = parse_reply(response(), cross_request())
    asyncio.run(monitor.run_once())
    records = discoveries(monitor)
    assert len(records) == 1 and records[0].reason == 'insufficient_locations'
    assert records[0].subject_key is None and records[0].incident_id is None


def test_matched_scene_opens_one_case_without_an_extra_cloud_call_or_fake_answer():
    monitor, _, provider = scene_setup()
    asyncio.run(monitor.run_once())
    events = monitor.drain_events()
    d = next(e.discovery for e in events if e.discovery)
    assert d.reason == 'matched' and d.subject_key == 'person-1'
    incident = monitor.incident(d.incident_id)
    assert incident.answer is None and incident.attempts == 1 and not incident.pending
    assert incident.auto_normal_blocked and incident.candidate_sources == ('cloud_crosscheck',)
    assert sum(e.kind == 'question_requested' for e in events) == 1
    assert not any(e.kind == 'notification_requested' for e in events)
    assert not asyncio.run(monitor.run_once()) and len(provider.calls) == 1


def test_two_people_two_incidents_and_duplicate_finding_does_not_repeat_question():
    monitor, clock, provider = make()
    enable(monitor)
    for t in (159.5, 160):
        feed(monitor, clock, t, (pose(), pose('other', HELPER)))
    provider.reply = reply(finding(), finding(HELPER), finding())
    asyncio.run(monitor.run_once())
    events = monitor.drain_events()
    ds = [e.discovery for e in events if e.discovery]
    assert len({d.incident_id for d in ds}) == 2
    assert sum(e.kind == 'question_requested' for e in events) == 2
    assert len(provider.calls) == 1


def test_existing_yolo_case_merges_and_recheck_budget_does_not_reset():
    monitor, clock, provider = make()
    enable(monitor)
    feed(monitor, clock, 100)
    iid = monitor.candidate(candidate())
    provider.reply = CloudFallReply(VideoAssessment.SUSPECTED_FALL, 'x')
    asyncio.run(monitor.run_once())
    old = monitor.incident(iid)
    monitor.drain_events()
    for n in range(1, 121):
        feed(monitor, clock, 100 + n * 0.5)
    provider.reply = reply(finding())
    asyncio.run(monitor.run_once())
    events = monitor.drain_events()
    assert next(e.discovery for e in events if e.discovery).incident_id == iid
    assert not any(e.kind in {'question_requested', 'incident_opened'} for e in events)
    assert monitor.incident(iid).question_id == old.question_id
    assert (monitor.incident(iid).attempts, monitor.incident(iid).rechecks) == (1, 0)
    assert monitor.incident(iid).candidate_sources == ('yolo_pose', 'cloud_crosscheck')


def test_same_track_id_after_a_gap_does_not_prove_existing_incident_identity():
    monitor, clock, provider = make()
    enable(monitor)
    feed(monitor, clock, 100)
    iid = monitor.candidate(candidate())
    asyncio.run(monitor.run_once())
    monitor.drain_events()
    for t in (159.5, 160):
        feed(monitor, clock, t)
    provider.reply = reply(finding())
    asyncio.run(monitor.run_once())
    d = discoveries(monitor)[0]
    assert d.reason == 'incident_target_continuity_unverified' and d.incident_id is None
    assert monitor.incident(iid).candidate_sources == ('yolo_pose',)


def test_candidate_before_pose_callback_captures_exact_frame_continuity():
    monitor, clock, provider = make()
    enable(monitor)
    monitor.ingest_rgb(frame(100))
    iid = monitor.candidate(candidate())
    assert monitor.incident(iid).subject_association_token is None
    monitor.ingest_subject_frame(SubjectFrame(100, (pose(),), 0.5))
    assert monitor.incident(iid).subject_association_token is not None
    provider.reply = CloudFallReply(VideoAssessment.SUSPECTED_FALL, 'x')
    asyncio.run(monitor.run_once())
    monitor.drain_events()
    for n in range(1, 121):
        feed(monitor, clock, 100 + n * 0.5)
    provider.reply = reply(finding())
    asyncio.run(monitor.run_once())
    assert discoveries(monitor)[0].incident_id == iid


def test_normal_scene_cannot_resolve_previously_discovered_suspicion():
    monitor, clock, provider = scene_setup()
    asyncio.run(monitor.run_once())
    iid = discoveries(monitor)[0].incident_id
    answer(monitor, iid, VoiceAnswer.OKAY)
    for t in (219.5, 220):
        feed(monitor, clock, t)
    provider.reply = CloudFallReply(VideoAssessment.NORMAL_ACTIVITY, '일반 행동')
    asyncio.run(monitor.run_once())
    assert monitor.incident(iid).close_reason is None
    assert monitor.incident(iid).video.assessment is VideoAssessment.SUSPECTED_FALL


def test_scene_fall_not_assigned_to_suspected_person_when_localization_contradicts():
    value = raw_scene()
    value['assessment'] = 'observed_fall'
    result = parse_reply(response(json.dumps(value)), cross_request())
    assert result.assessment is VideoAssessment.OBSERVED_FALL
    assert result.localization_failed and result.findings == ()


def test_help_answer_is_not_cleared_by_new_cloud_evidence():
    monitor, clock, provider = scene_setup()
    asyncio.run(monitor.run_once())
    iid = discoveries(monitor)[0].incident_id
    answer(monitor, iid, VoiceAnswer.HELP)
    monitor.drain_events()
    for n in range(1, 121):
        feed(monitor, clock, 160 + n * 0.5)
    provider.reply = reply(finding(assessment=VideoAssessment.OBSERVED_FALL))
    asyncio.run(monitor.run_once())
    assert monitor.incident(iid).answer is VoiceAnswer.HELP
    assert monitor.incident(iid).state.value == 'help_required'
    assert not any(e.kind == 'question_requested' for e in monitor.drain_events())


@pytest.mark.parametrize('during,expected', [
    ('lost', 'current_target_unavailable'), ('gap', 'current_target_unavailable'),
    ('invalid', 'current_target_unavailable'), ('stale', 'current_target_unavailable'),
    ('new_case', 'incident_changed_during_scan'),
])
def test_changes_while_cloud_pending_do_not_attach_old_result(during, expected):
    async def run():
        monitor, clock, provider = scene_setup()
        provider.release = asyncio.Event()
        task = asyncio.create_task(monitor.run_once())
        await provider.started.wait()
        if during == 'lost':
            feed(monitor, clock, 160.5, ())
        elif during == 'gap':
            feed(monitor, clock, 161)
        elif during == 'invalid':
            monitor.invalidate_subject_input()
        elif during == 'stale':
            clock.value = 180
        else:
            monitor.candidate(candidate(t=160))
            monitor.drain_events()
        provider.release.set()
        await task
        events = monitor.drain_events()
        d = next(e.discovery for e in events if e.discovery)
        assert d.reason == expected and d.incident_id is None
        assert not any(e.kind == 'question_requested' for e in events)
    asyncio.run(run())


def test_latency_uses_dispatch_boxes_and_real_capture_time_not_current_geometry():
    async def run():
        monitor, clock, provider = scene_setup()
        provider.release = asyncio.Event()
        task = asyncio.create_task(monitor.run_once())
        await provider.started.wait()
        # Ten seconds of healthy tracking while inference is in flight; current
        # boxes moved away. The original two image boxes must be used instead.
        for n in range(1, 21):
            feed(monitor, clock, 160 + n * 0.5, (pose(box=HELPER),))
        provider.release.set()
        await task
        d = discoveries(monitor)[0]
        assert d.reason == 'matched'
        incident = monitor.incident(d.incident_id)
        assert incident.last_observed_at == 160 and incident.opened_at == 170
    asyncio.run(run())


def test_consent_revocation_discards_inflight_findings():
    async def run():
        monitor, _, provider = scene_setup()
        provider.release = asyncio.Event()
        task = asyncio.create_task(monitor.run_once())
        await provider.started.wait()
        enable(monitor, consent=False)
        provider.release.set()
        await task
        assert not discoveries(monitor)
    asyncio.run(run())


def test_unidentified_records_survive_restart_and_are_not_fake_web_incidents(tmp_path):
    monitor, _, provider = scene_setup()
    path = tmp_path / 'private' / 'fall.sqlite'
    journal = SqliteFallJournal(path, device_id='robot')
    monitor._journal = journal
    provider.reply = reply(replace(finding(), regions=()))
    asyncio.run(monitor.run_once())
    event = next(e for e in monitor.drain_events() if e.discovery)
    assert event_metadata(event)['discovery']['subject_key'] is None
    assert journal.pending() is None and journal.unresolved() == []
    journal.close()
    reopened = SqliteFallJournal(path, device_id='robot')
    try:
        rows = reopened.discoveries()
        assert len(rows) == 1 and rows[0]['incident_id'] is None
        assert rows[0]['assessment'] == 'suspected_fall'
        assert 'private-model-text' not in json.dumps(rows) and 'jpeg' not in json.dumps(rows)
        assert reopened.discoveries(after_sequence=rows[0]['sequence']) == []
    finally:
        reopened.close()


def test_discovery_storage_failure_is_fail_closed(tmp_path):
    monitor, _, provider = scene_setup()
    journal = SqliteFallJournal(tmp_path / 'private' / 'fall.sqlite', device_id='robot')
    monitor._journal = journal
    journal.close()
    provider.reply = reply(replace(finding(), regions=()))
    with pytest.raises(FallJournalError):
        asyncio.run(monitor.run_once())
    assert not discoveries(monitor)
    with pytest.raises(FallJournalError):
        enable(monitor)


def test_capacity_exhaustion_retains_unidentified_finding():
    monitor, clock, provider = make(max_incidents=1)
    enable(monitor)
    feed(monitor, clock, 100)
    monitor.candidate(candidate(subject='other'))
    asyncio.run(monitor.run_once())
    monitor.drain_events()
    for t in (159.5, 160):
        feed(monitor, clock, t)
    provider.reply = reply(finding())
    asyncio.run(monitor.run_once())
    d = discoveries(monitor)[0]
    assert d.reason == 'incident_capacity' and d.incident_id is None


def test_new_observed_fall_invalidates_old_okay_but_not_help_or_fall_history():
    monitor, clock, provider = scene_setup()
    asyncio.run(monitor.run_once())
    iid = discoveries(monitor)[0].incident_id
    answer(monitor, iid, VoiceAnswer.OKAY)
    old = monitor.incident(iid)
    for n in range(1, 121):
        feed(monitor, clock, 160 + n * 0.5)
    provider.reply = reply(finding(assessment=VideoAssessment.OBSERVED_FALL))
    asyncio.run(monitor.run_once())
    incident = monitor.incident(iid)
    assert incident.revision == old.revision + 1 and incident.answer is None
    assert incident.fall_seen and incident.question_id != old.question_id
    assert (incident.attempts, incident.rechecks) == (old.attempts, old.rechecks)
    for n in range(1, 121):
        feed(monitor, clock, 220 + n * 0.5)
    provider.reply = reply(finding())
    asyncio.run(monitor.run_once())
    assert monitor.incident(iid).video.assessment is VideoAssessment.OBSERVED_FALL
    assert monitor.incident(iid).fall_seen
