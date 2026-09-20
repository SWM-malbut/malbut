import asyncio
import io
import json
import sqlite3
import stat
from types import SimpleNamespace
import urllib.error

import pytest

from malbut_agent_server.adapters.outbound.homecam_fall_events import (
    FallEventUploader, FallUploadError, HomecamFallEventClient,
)
from malbut_agent_server.adapters.outbound.sqlite_fall_journal import SqliteFallJournal
from malbut_agent_server.domain.fall_monitoring import CloudFallReply, VideoAssessment, VoiceAnswer
from malbut_agent_server.ports.fall_event_journal import FallJournalError
from test_cloud_fall_monitor import make, enable, frame, candidate, answer
from test_fall_normal_closure import subject_check


def attach(tmp_path):
    monitor, clock, provider = make()
    now = [1789689600.0]
    journal = SqliteFallJournal(
        tmp_path / 'private' / 'fall.sqlite', device_id='robot', wall_clock=lambda: now[0])
    monitor._journal = journal
    enable(monitor)
    return monitor, clock, provider, journal, now


def test_runtime_commits_notice_before_exposing_it_and_survives_reopen(tmp_path):
    monitor, clock, provider, journal, now = attach(tmp_path)
    monitor.ingest_rgb(frame(clock()))
    iid = monitor.candidate(candidate())
    answer(monitor, iid, VoiceAnswer.HELP)
    events = monitor.drain_events()
    notice = next(e for e in events if e.kind == 'notification_requested')
    assert any(row['event_id'] == notice.event_id for row in journal.upload_status())
    assert json.loads(journal.pending()['payload'])['notificationLevel'] == 'urgent'
    path = tmp_path / 'private' / 'fall.sqlite'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    journal.close()
    reopened = SqliteFallJournal(path, device_id='robot', wall_clock=lambda: now[0])
    try:
        assert reopened.pending() is not None
        unresolved = reopened.unresolved()
        assert unresolved[0]['incidentId'] == iid
        assert unresolved[0]['answer'] == 'help_request'
        assert unresolved[0]['state'] == 'help_required'
        # Upload acknowledgement removes work from the queue, not the history.
        ids = [r['event_id'] for r in reopened.upload_status()]
        for event_id in ids:
            reopened.acknowledge(event_id)
        assert reopened.pending() is None
        assert len(reopened.unresolved()) == 1
    finally:
        reopened.close()


def test_fall_record_kept_without_rgb_transcript_or_model_explanation(tmp_path):
    monitor, clock, provider, journal, _ = attach(tmp_path)
    try:
        monitor.ingest_rgb(frame(clock()))
        iid = monitor.candidate(candidate())
        provider.reply = CloudFallReply(VideoAssessment.OBSERVED_FALL, 'private-model-text')
        asyncio.run(monitor.run_once())
        answer(monitor, iid, VoiceAnswer.OKAY)
        records = []
        while (row := journal.pending()) is not None:
            records.append(json.loads(row['payload']))
            journal.acknowledge(row['event_id'])
        assert records[-1]['fallSeen'] is True
        assert records[-1]['answer'] == 'okay'
        assert 'private-model-text' not in json.dumps(records)
        assert 'jpeg' not in json.dumps(records)
        notice_index = next(i for i, r in enumerate(records) if r['notificationLevel'] == 'info')
        voice_index = next(i for i, r in enumerate(records) if r['eventKind'] == 'voice_result')
        assert notice_index < voice_index
    finally:
        journal.close()


def test_storage_failure_stops_monitor_without_exposing_unpersisted_events(tmp_path):
    monitor, _, _, journal, _ = attach(tmp_path)
    journal.close()
    with pytest.raises(FallJournalError):
        monitor.candidate(candidate())
    assert monitor.drain_events() == ()
    assert not asyncio.run(monitor.run_once())
    with pytest.raises(FallJournalError, match='recover'):
        enable(monitor)


def test_normal_closure_keeps_proof_locally_and_upload_contract_unchanged(tmp_path):
    monitor, clock, provider, journal, now = attach(tmp_path)
    path = tmp_path / 'private' / 'fall.sqlite'
    iid = monitor.candidate(candidate())
    answer(monitor, iid, VoiceAnswer.OKAY)
    monitor.ingest_rgb(frame(clock()))
    asyncio.run(monitor.run_once())
    clock.value += 3
    monitor.ingest_rgb(frame(clock()))
    asyncio.run(monitor.run_once())
    monitor.observe_subject(subject_check(monitor, iid, clock))
    events = monitor.drain_events()
    assert events[-1].kind == 'incident_resolved'
    assert journal.unresolved() == []
    count = len(journal.upload_status())
    journal.close()
    reopened = SqliteFallJournal(path, device_id='robot', wall_clock=lambda: now[0])
    try:
        assert len(reopened.upload_status()) == count
        assert reopened.unresolved() == []
        with sqlite3.connect(path) as db:
            payload, proof = db.execute(
                'SELECT payload,closure_evidence FROM incident_events '
                'WHERE event_id=?', (events[-1].event_id,)).fetchone()
        proof = json.loads(proof)
        assert len(proof['normalChecks']) == 2
        assert proof['subjectObservation']['associationVerified']
        assert proof['candidateSources'] == ['yolo_pose']
        assert 'normalChecks' not in json.loads(payload)
        assert 'jpeg' not in json.dumps(proof)
        assert len(json.loads(payload)) == 14  # Existing strict web contract.
    finally:
        reopened.close()


def test_journal_adds_optional_proof_column_to_existing_database(tmp_path):
    path = tmp_path / 'private' / 'fall.sqlite'
    path.parent.mkdir(mode=0o700)
    path.touch(mode=0o600)
    with sqlite3.connect(path) as db:
        db.execute('''CREATE TABLE incident_events(
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
            incident_id TEXT NOT NULL, payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at REAL NOT NULL DEFAULT 0, last_error TEXT)''')
        db.execute("INSERT INTO incident_events(event_id,incident_id,payload) VALUES('e','i','{}')")
    journal = SqliteFallJournal(path, device_id='robot')
    try:
        assert journal.pending()['event_id'] == 'e'
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT closure_evidence FROM incident_events').fetchone() == (None,)
    finally:
        journal.close()


def test_wrong_device_and_insecure_paths_rejected_without_changing_identity(tmp_path):
    path = tmp_path / 'private' / 'fall.sqlite'
    journal = SqliteFallJournal(path, device_id='robot')
    journal.close()
    with pytest.raises(ValueError, match='different device'):
        SqliteFallJournal(path, device_id='other')
    reopened = SqliteFallJournal(path, device_id='robot')
    reopened.close()
    link = tmp_path / 'link.sqlite'
    link.symlink_to(path)
    with pytest.raises(ValueError):
        SqliteFallJournal(link, device_id='robot')
    public = tmp_path / 'public'
    public.mkdir(mode=0o755)
    with pytest.raises(ValueError, match='private'):
        SqliteFallJournal(public / 'fall.sqlite', device_id='robot')


def test_uploader_retries_same_payload_and_only_acknowledges_durable_server_store(tmp_path):
    monitor, _, _, journal, now = attach(tmp_path)
    try:
        monitor.candidate(candidate())
        original = journal.pending()
        received = []

        def offline(payload):
            received.append(payload)
            raise FallUploadError('http_503')

        client = SimpleNamespace(store=offline)
        uploader = FallEventUploader(journal, client)
        assert uploader.run_once()
        assert original['event_id'] in [r['event_id'] for r in journal.upload_status()]
        now[0] += 301
        client.store = received.append
        assert uploader.run_once()
        assert received[0] == received[1]
        assert journal.upload_status()[0]['status'] == 'stored'
    finally:
        journal.close()


def test_contract_conflict_is_blocked_not_rewritten_with_new_id(tmp_path):
    monitor, _, _, journal, _ = attach(tmp_path)
    try:
        monitor.candidate(candidate())

        def conflict(payload):
            raise FallUploadError('http_409', blocked=True)

        uploader = FallEventUploader(journal, SimpleNamespace(store=conflict))
        uploader.run_once()
        assert journal.upload_status()[0]['status'] == 'blocked'
        assert journal.upload_status()[0]['last_error'] == 'http_409'
    finally:
        journal.close()


def client():
    return HomecamFallEventClient(
        base_url='https://homecam.example.com', device_id='robot',
        allowed_hosts={'homecam.example.com'},
        device_token='secret-token')


@pytest.mark.parametrize('url', [
    'http://homecam.example.com', 'https://evil.example.com',
    'https://user:password@homecam.example.com', 'https://homecam.example.com/?to=other',
    'https://homecam.example.com/api', 'https://homecam.example.com:444',
])
def test_upload_origin_allowlist(url):
    with pytest.raises(ValueError):
        HomecamFallEventClient(
            base_url=url, device_id='robot', allowed_hosts={'homecam.example.com'},
            device_token='secret-token')


def test_http_client_requires_matching_storage_ack_and_does_not_leak_token():
    instance = client()
    response = io.BytesIO(b'{"stored":true,"eventId":"e1","push":{"accepted":false}}')
    response.status = 201
    requests = []

    def open_request(request, timeout):
        requests.append(request)
        assert timeout == 10
        return response

    instance._opener.open = open_request
    instance.store('{"eventId":"e1"}')
    assert requests[0].full_url == 'https://homecam.example.com/api/device/v1/fall-events'
    assert requests[0].get_header('Authorization') == 'Bearer secret-token'
    assert requests[0].get_header('X-malbut-device-id') == 'robot'
    assert 'secret-token' not in repr(instance)
    for body in (b'{}', b'{"stored":true,"eventId":"other"}', b'[]'):
        response = io.BytesIO(body)
        response.status = 200
        with pytest.raises(FallUploadError):
            instance.store('{"eventId":"e1"}')


def test_http_redirect_and_errors_are_sanitized():
    instance = client()
    no_redirect = next(h for h in instance._opener.handlers if hasattr(h, 'redirect_request'))
    assert no_redirect.redirect_request(None, None, 302, '', {}, 'https://evil.example.com') is None
    for code, blocked in ((409, True), (401, True), (503, False), (302, False)):
        def failure(*args, **kwargs):
            raise urllib.error.HTTPError('secret-url', code, 'secret-token', {}, None)
        instance._opener.open = failure
        with pytest.raises(FallUploadError) as caught:
            instance.store('{"eventId":"e1"}')
        assert caught.value.blocked is blocked
        assert 'secret' not in str(caught.value)


def test_worker_default_does_not_read_token_create_journal_or_call_http(tmp_path, capsys):
    from malbut_agent_server.fall_upload_worker import main
    path = tmp_path / 'private' / 'not-created.sqlite'
    assert main(['--journal', str(path), '--device-id', 'robot',
                 '--base-url', 'https://homecam.example.com',
                 '--allow-host', 'homecam.example.com',
                 '--token-file', str(tmp_path / 'missing-token')]) == 0
    assert not path.exists()
    assert 'no upload' in capsys.readouterr().out


def test_only_explicit_auth_retry_requeues_and_conflicts_remain_blocked(tmp_path):
    monitor, _, _, journal, _ = attach(tmp_path)
    try:
        monitor.candidate(candidate())
        rows = journal.upload_status()
        journal.failed(rows[0]['event_id'], code='http_401', blocked=True)
        journal.failed(rows[1]['event_id'], code='http_409', blocked=True)
        journal.retry_auth_failed()
        assert [r['status'] for r in journal.upload_status()] == ['pending', 'blocked']
    finally:
        journal.close()


def test_token_file_permissions_and_no_symlink(tmp_path):
    from malbut_agent_server.fall_upload_worker import _read_token
    token = tmp_path / 'token'
    token.write_text('test-token\n')
    token.chmod(0o600)
    assert _read_token(token) == 'test-token'
    token.chmod(0o644)
    with pytest.raises(ValueError):
        _read_token(token)
    token.chmod(0o600)
    link = tmp_path / 'token-link'
    link.symlink_to(token)
    with pytest.raises(OSError):
        _read_token(link)
