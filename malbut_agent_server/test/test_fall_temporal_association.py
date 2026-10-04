"""Conservative timestamp bounds and late-Pose safety, without models/network."""

import asyncio
from dataclasses import replace
import json

import pytest

from malbut_agent_server.application.fall_cloud_association import (
    associate_timed_finding, supplement_samples,
)
from malbut_agent_server.application.fall_subject_evidence import FallSubjectEvidence
from malbut_agent_server.adapters.outbound.sqlite_fall_journal import SqliteFallJournal
from malbut_agent_server.domain.fall_monitoring import (
    CloudFallReply, CloudPersonRegion, CloudPoseLink, SubjectFrame, VoiceAnswer,
)
from malbut_agent_server.ports.fall_event_journal import FallJournalError
from test_fall_cloud_association import HELPER, finding, pose as subject
from test_fall_association_input_timing import BOX as INPUT_BOX, prepare, pose, pose_payload, rgb
from test_cloud_fall_monitor import make
from malbut_agent_server.application.fall_detector_input import FallDetectorInput


def bank():
    return FallSubjectEvidence(retention_s=10, max_frames=64)


@pytest.mark.parametrize('offset,expected', [
    (.02, 'matched'), (.05, 'matched'), (.1, 'matched'),
    (.1001, 'no_matching_track'), (.15, 'no_matching_track'),
])
def test_two_sided_time_bound(offset, expected):
    evidence = bank()
    for stamp in (10-offset, 10+offset, 10.4-offset, 10.4+offset):
        evidence.append(SubjectFrame(stamp, (subject(),), .5))
    result = associate_timed_finding(finding(), evidence.association_samples((10, 10.4)))
    assert result.reason == expected


@pytest.mark.parametrize('mode,expected', [
    ('one_side', 'no_matching_track'),
    ('explicit_empty', 'no_matching_track'),
    ('weak', 'track_unusable'),
    ('competing', 'ambiguous_tracks'),
    ('different_person', 'track_changed'),
    ('moved_away', 'no_matching_track'),
    ('gap', 'track_changed'),
])
def test_nearby_pose_cannot_override_negative_or_ambiguous_evidence(mode, expected):
    evidence = bank()
    for stamp in (9.9, 10., 10.1, 10.3, 10.4, 10.5):
        if stamp in (10., 10.4) and mode != 'explicit_empty':
            continue
        if mode == 'one_side' and stamp in (10.1, 10.5):
            continue
        people = (subject(),)
        if mode == 'explicit_empty' and stamp in (10., 10.4):
            people = ()
        if mode == 'weak' and stamp == 10.1:
            people = (subject(usable=False),)
        if mode == 'competing' and stamp == 10.1:
            people += (subject('other'),)
        if mode == 'different_person' and stamp >= 10.3:
            people = (subject('other'),)
        if mode == 'moved_away' and stamp == 10.1:
            people = (subject(box=HELPER),)
        evidence.append(SubjectFrame(stamp, people, .15 if mode == 'gap' else .5))
    assert associate_timed_finding(
        finding(), evidence.association_samples((10., 10.4))).reason == expected


def test_no_temporal_permission_for_exact_only_normal_closure_target():
    evidence = bank()
    from test_cloud_fall_monitor import frame
    from malbut_agent_server.domain.fall_monitoring import FrameWindow
    for stamp in (9.9, 10.1, 10.3, 10.5):
        evidence.append(SubjectFrame(stamp, (subject(),), .5))
    window = FrameWindow((frame(10), frame(10.4)), 10, 10.4, False)
    assert evidence.target('person-1', window) is None
    assert evidence.snapshot(window) == ((), ())


def test_dispatch_observation_is_not_overwritten_by_newer_boxes():
    evidence = bank()
    evidence.append(SubjectFrame(10, (), .5))
    old = evidence.association_samples((10,))
    assert old == (((10, ()),),)
    fake_new = (((10, (('other', 'new-token', subject('other')),)),),)
    assert supplement_samples(old, fake_new) == old


def test_snapshot_size_mismatch_is_not_silently_truncated():
    with pytest.raises(ValueError):
        supplement_samples(((), ()), ((),))


def test_four_cloud_regions_check_all_eight_neighbors():
    evidence = bank()
    times = (10., 10.4, 10.8, 11.2)
    for stamp in times:
        for measured in (stamp-.1, stamp+.1):
            evidence.append(SubjectFrame(measured, (subject(),), .5))
    regions = tuple(CloudPersonRegion(i, finding().regions[0].box) for i in range(4))
    assert associate_timed_finding(
        replace(finding(), regions=regions), evidence.association_samples(times)).reason == 'matched'


@pytest.mark.parametrize('arrival', ['during_cloud', 'after_cloud'])
def test_new_pose_candidate_from_requested_video_links_without_another_cloud_call(arrival):
    async def run():
        monitor, clock, provider = make(clip_window_s=5)
        adapter = FallDetectorInput(monitor, max_source_age_s=2)
        adapter.configure(enabled=True, camera_enabled=True,
                          cloud_consent=True, connected=True)
        for index in range(26):
            capture = 1055 + index * .2
            rgb(adapter, clock, capture)
            if index < 25:
                pose(adapter, clock, capture)
        clock.value = 160.1
        person = replace(finding(), regions=(
            CloudPersonRegion(0, INPUT_BOX), CloudPersonRegion(11, INPUT_BOX)))
        provider.reply = CloudFallReply(person.assessment, 'fixture', (person,))
        if arrival == 'during_cloud':
            provider.release = asyncio.Event()
            task = asyncio.create_task(monitor.run_once())
            await asyncio.wait_for(provider.started.wait(), timeout=2)
            try:
                iid, = pose(adapter, clock, 1060, received_at=1060.15, initial=True)
            finally:
                provider.release.set()
            assert await task
        else:
            assert await monitor.run_once()
            iid, = pose(adapter, clock, 1060, received_at=1060.15, initial=True)
        events = monitor.drain_events()
        links = [e.discovery for e in events if e.discovery and e.discovery.subject_key]
        assert len(links) == 1 and links[0].incident_id == iid
        expected_cases = 1 if arrival == 'during_cloud' else 2
        assert sum(e.kind == 'incident_opened' for e in events) == expected_cases
        assert len({e.question_id for e in events
                    if e.kind == 'question_requested'}) == expected_cases
        assert not monitor.incident(iid).pending
        assert monitor.incident(iid).attempts == 1
        assert not await monitor.run_once()
        assert len(provider.calls) == 1
    asyncio.run(run())


def test_multiple_findings_cannot_late_attach_to_one_target():
    async def run():
        adapter, monitor, clock, provider, iid, _, _, person = await prepare('late_after_cloud')
        provider.reply = replace(provider.reply, findings=(person, person))
        await monitor.run_once()
        initial = [e.discovery for e in monitor.drain_events() if e.discovery]
        assert len(initial) == 2 and all(d.subject_key is None for d in initial)
        pose(adapter, clock, 1060, received_at=1060.15)
        links = [e.discovery for e in monitor.drain_events() if e.kind == 'cloud_discovery_linked']
        assert len(links) == 1 and links[0].incident_id == iid
        assert sum(e.discovery.subject_key is None for e in monitor._discoveries.values()) == 1
        assert monitor.incident(initial[0].incident_id).subject_key is None
    asyncio.run(run())


def test_pose_reset_during_cloud_does_not_join_old_and_new_generations():
    async def run():
        adapter, monitor, clock, provider, _, _, _, _ = await prepare('late_during_cloud')
        provider.release = asyncio.Event()
        task = asyncio.create_task(monitor.run_once())
        await asyncio.wait_for(provider.started.wait(), timeout=2)
        try:
            monitor.invalidate_subject_input()
            pose(adapter, clock, 1060, received_at=1060.15)
        finally:
            provider.release.set()
        await task
        discoveries = [e.discovery for e in monitor.drain_events() if e.discovery]
        assert len(discoveries) == 1 and discoveries[0].subject_key is None
    asyncio.run(run())


@pytest.mark.parametrize('change', [
    'consent', 'camera', 'disabled', 'disconnected', 'control', 'invalid_pose',
    'source_closed', 'target_closed', 'target_answered', 'expired', 'other_person',
])
def test_late_pose_does_not_bypass_lifecycle_or_identity_guards(change):
    async def run():
        adapter, monitor, clock, provider, iid, _, _, _ = await prepare('late_after_cloud')
        assert await monitor.run_once()
        d = next(e.discovery for e in monitor.drain_events() if e.discovery)
        original_calls = len(provider.calls)
        if change in {'consent', 'camera', 'disabled', 'disconnected'}:
            adapter.configure(enabled=change != 'disabled', camera_enabled=change != 'camera',
                              cloud_consent=change != 'consent', connected=change != 'disconnected')
        elif change == 'control':
            monitor.set_cloud_block('control_unavailable')
        elif change == 'invalid_pose':
            monitor.invalidate_subject_input()
        elif change in {'source_closed', 'target_closed'}:
            monitor.resolve(d.incident_id if change == 'source_closed' else iid,
                            revision=1, reason='response_completed')
        elif change == 'target_answered':
            target = monitor.incident(iid)
            monitor.confirmation_result(
                incident_id=iid, question_id=target.question_id, subject_key=target.subject_key,
                evidence_revision=target.revision, situation_assessment='unknown', help_needed=True)
        monitor.drain_events()
        if change == 'expired':
            # Accept the missing original frame at the input boundary, so
            # this specifically checks the 2s retry deadline, not frame age.
            adapter.max_source_age_s = 3
            monitor.policy = replace(monitor.policy, max_person_observation_age_s=3)
            pose(adapter, clock, 1060, received_at=1062.22)
            assert monitor._subject_evidence.latest('pose:0:p1')[0] == 160
        elif change == 'other_person':
            data = json.loads(pose_payload(1060))
            data['tracks'][0]['targetTrackId'] = 'other'
            clock.value = 160.15
            adapter.candidates(json.dumps(data), source_now=1060.15, now=clock())
        else:
            pose(adapter, clock, 1060, received_at=1060.15)
        assert not any(e.kind == 'cloud_discovery_linked' for e in monitor.drain_events())
        assert len(provider.calls) == original_calls
    asyncio.run(run())


def test_late_pose_persists_original_and_link_without_transferring_scene_answer(tmp_path):
    async def run():
        adapter, monitor, clock, provider, iid, qid, _, _ = await prepare('late_after_cloud')
        journal = SqliteFallJournal(tmp_path / 'private' / 'events.sqlite', device_id='robot')
        monitor._journal = journal
        try:
            await monitor.run_once()
            d = next(e.discovery for e in monitor.drain_events() if e.discovery)
            scene = monitor.incident(d.incident_id)
            assert monitor.confirmation_result(
                incident_id=scene.incident_id, question_id=scene.question_id, subject_key=None,
                evidence_revision=1, situation_assessment='resolved', help_needed=False)
            monitor.drain_events()
            pose(adapter, clock, 1060, received_at=1060.15)
            records = journal.discoveries()
            assert len(records) == 2
            assert records[0]['subject_key'] is None
            assert records[1]['incident_id'] == iid
            assert records[1]['association_link']['method'] == 'measured_pose_timestamps_v1'
            assert monitor.incident(iid).answer is None
            assert monitor.incident(iid).question_id == qid
            assert monitor.incident(scene.incident_id).answer is VoiceAnswer.UNCLEAR
            assert len(provider.calls) == 2
        finally:
            journal.close()
        reopened = SqliteFallJournal(tmp_path / 'private' / 'events.sqlite', device_id='robot')
        try:
            assert reopened.discoveries() == records
        finally:
            reopened.close()
    asyncio.run(run())


def test_failed_late_link_journal_rolls_back_before_exposing_association(tmp_path):
    async def run():
        adapter, monitor, clock, _, iid, _, _, _ = await prepare('late_after_cloud')
        journal = SqliteFallJournal(tmp_path / 'private' / 'events.sqlite', device_id='robot')
        monitor._journal = journal
        try:
            await monitor.run_once()
            monitor.drain_events()
            target = monitor.incident(iid)
            original = journal._append_discovery
            def fail(**kwargs):
                original(**kwargs)
                raise OSError('fixture full disk')
            journal._append_discovery = fail
            with pytest.raises(FallJournalError):
                pose(adapter, clock, 1060, received_at=1060.15)
            assert len(journal.discoveries()) == 1
            assert monitor.incident(iid).candidate_sources == target.candidate_sources
            assert not any(e.kind == 'cloud_discovery_linked' for e in monitor.drain_events())
        finally:
            journal.close()
    asyncio.run(run())


@pytest.mark.parametrize('poses', [((10.05,), (10.4,)), ((9.8, 10.1), (10.4,)),
                                   ((10.1, 9.9), (10.4,))])
def test_link_proof_rejects_extrapolation_or_out_of_bound(poses):
    with pytest.raises(ValueError):
        CloudPoseLink('source', 1, 'token', 11, (10, 10.4), poses)
