"""History provenance only; scripted replies and real journal, no paid calls."""

import asyncio
from dataclasses import replace
import json

import pytest

from malbut_agent_server.adapters.outbound.sqlite_fall_journal import SqliteFallJournal
from malbut_agent_server.domain.fall_monitoring import CloudFallReply, VideoAssessment
from malbut_agent_server.fall_runtime import event_metadata
from test_cloud_fall_monitor import candidate, enable, frame, make
from test_fall_detector_input import candidates, item, make_input
from test_fall_cloud_association import finding, scene_setup
from test_fall_deferred_association import setup, start, link
from test_fall_pending_association import pending_case, advance


def records(journal):
    return [json.loads(row[0]) for row in journal._db.execute(
        'SELECT payload FROM incident_events ORDER BY sequence')]


@pytest.mark.parametrize('kind,reasons,expected', [
    ('fall_suspected', ['rapid_posture_change'], 'pose_rapid_posture_change'),
    ('found_down', ['sustained_low_posture', 'horizontal_torso_and_body'], 'pose_sustained_horizontal_posture'),
    ('found_down', ['sustained_low_posture'], 'pose_sustained_low_posture'),
    ('found_down', ['compact_shoulder_hip_leg_layout'], 'pose_sustained_low_posture'),
    ('found_down', ['invented_model_reason'], 'pose_sustained_low_posture'),
    ('found_down', 'horizontal_torso_and_body', 'pose_sustained_low_posture'),
    ('found_down', None, 'pose_sustained_low_posture'),
])
def test_actual_pose_reason_reaches_open_and_update_without_promoting_to_fall(kind, reasons, expected):
    adapter, monitor, clock, provider = make_input()
    data = item() | dict(candidateKind=kind, reasons=reasons)
    iid, = adapter.candidates(candidates(data), source_now=1000, now=clock())
    opened, = [e for e in monitor.drain_events() if e.kind == 'incident_opened']
    assert opened.reason == expected and monitor.incident(iid).video is None
    assert not provider.calls and monitor.incident(iid).fall_seen is False
    clock.value += 1
    data = item(revision=2, end=1001) | dict(candidateKind=kind, reasons=reasons)
    adapter.candidates(candidates(data), source_now=1001, now=clock())
    updated, = [e for e in monitor.drain_events() if e.kind == 'incident_updated']
    assert updated.reason == expected


@pytest.mark.parametrize('missing_pose', [False, True])
def test_crosscheck_history_keeps_actual_scene_explanation_and_source(tmp_path, missing_pose):
    monitor, _, provider = scene_setup()
    found = replace(finding(), regions=()) if missing_pose else finding()
    text = '거울 쪽 형상이 누운 사람처럼 보인다는 모의 설명'
    provider.reply = CloudFallReply(VideoAssessment.SUSPECTED_FALL, text, (found,))
    journal = SqliteFallJournal(tmp_path / 'private' / 'events.sqlite', device_id='robot')
    monitor._journal = journal
    try:
        assert asyncio.run(monitor.run_once())
        history = records(journal)
        opened = next(r for r in history if r['eventKind'] == 'incident_opened')
        assert opened['reason'] == ('target_unidentified' if missing_pose else 'cloud_crosscheck')
        analysis, = [r['analysis'] for r in history if 'analysis' in r]
        assert analysis == dict(requestId=provider.calls[0].request_id, purpose='crosscheck',
                                assessment='suspected_fall', explanation=text)
        assert len(provider.calls) == 1
        assert all('analysis' not in r for r in history if r['eventKind'] != 'analysis_completed')
        # Model text is for human history only, never coordinator/action input
        # or the persistent person-association evidence contract.
        assert text not in json.dumps([event_metadata(e) for e in monitor.drain_events()])
        assert text not in json.dumps(journal.discoveries())
    finally:
        journal.close()


def test_deferred_link_keeps_original_explanation_instead_of_internal_placeholder(tmp_path):
    journal = SqliteFallJournal(tmp_path / 'private' / 'events.sqlite', device_id='robot')
    try:
        monitor, clock, provider, discoveries, _ = setup(journal)
        target_id, _ = link(monitor, clock, start(monitor, discoveries[0]))
        record = [r for r in records(journal) if r['incidentId'] == target_id
                  and r['eventKind'] == 'analysis_completed'][-1]
        assert record['analysis']['explanation'] == provider.reply.explanation
        assert record['analysis']['purpose'] == 'crosscheck'
        assert record['analysis']['requestId'] == discoveries[0].request_id
    finally:
        journal.close()


def test_pending_association_timeout_retains_original_explanation(tmp_path):
    journal = SqliteFallJournal(tmp_path / 'private' / 'events.sqlite', device_id='robot')
    try:
        monitor, clock, provider, _, discovery = pending_case(journal=journal)
        advance(monitor, clock, discovery.association_review.deadline)
        monitor.maintain_associations()
        latest = [r for r in records(journal) if r['eventKind'] == 'analysis_completed'][-1]
        assert latest['analysis']['requestId'] == discovery.request_id
        assert latest['analysis']['explanation'] == provider.reply.explanation
        assert latest['analysis']['purpose'] == 'crosscheck'
        assert len(provider.calls) == 2  # Existing calls only: initial + periodic.
    finally:
        journal.close()


@pytest.mark.parametrize('text', ['한' * 1000, '😀' * 1000])
def test_maximum_explanation_stays_within_upload_byte_limit(tmp_path, text):
    monitor, clock, provider = make()
    journal = SqliteFallJournal(tmp_path / 'private' / 'events.sqlite', device_id='robot')
    monitor._journal = journal
    try:
        enable(monitor)
        monitor.ingest_rgb(frame(clock()))
        monitor.candidate(candidate())
        provider.reply = CloudFallReply(VideoAssessment.SUSPECTED_FALL, text)
        asyncio.run(monitor.run_once())
        row = next(r for r in records(journal) if r['eventKind'] == 'analysis_completed')
        assert row['analysis']['explanation'] == text
        assert max(len(r[0].encode('utf-8')) for r in journal._db.execute(
            'SELECT payload FROM incident_events')) <= 8192
    finally:
        journal.close()


def test_bad_display_explanation_does_not_prevent_fall_analysis(tmp_path):
    monitor, clock, provider = make()
    journal = SqliteFallJournal(tmp_path / 'private' / 'events.sqlite', device_id='robot')
    monitor._journal = journal
    try:
        enable(monitor)
        monitor.ingest_rgb(frame(clock()))
        iid = monitor.candidate(candidate())
        # The real Cloud adapter limits this to 1000. A custom provider's
        # oversize explanation is omitted, never used to alter its judgment.
        provider.reply = CloudFallReply(VideoAssessment.OBSERVED_FALL, 'x' * 1001)
        assert asyncio.run(monitor.run_once())
        assert monitor.incident(iid).fall_seen
        assert 'analysis' not in next(r for r in records(journal) if r['eventKind'] == 'analysis_completed')
    finally:
        journal.close()
