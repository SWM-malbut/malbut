"""Metadata worker wiring: real temporary journal, mocked HTTPS, no robot or Cloud."""

import io
import json
from pathlib import Path
import sqlite3
import urllib.request

import pytest

from malbut_agent_server import fall_upload_worker as worker
from test_fall_cloud_association import finding, reply
from test_fall_journal import attach
from test_fall_unidentified_verification import scan


def args_for(tmp_path):
    token = tmp_path / 'device.token'
    token.write_text('private-test-device-token')
    token.chmod(0o600)
    return ['--journal', str(tmp_path / 'private/fall.sqlite'), '--device-id', 'robot',
            '--base-url', 'https://web.example.com', '--allow-host', 'web.example.com',
            '--token-file', str(token), '--execute', '--upload-clips']


def test_cloud_only_suspicion_uploads_event_clip_and_boxes_from_launch_arguments(tmp_path, monkeypatch):
    # Use the real Bringup argument composition, including the exact monitor DB.
    monkeypatch.syspath_prepend(str(Path(__file__).parents[2] / 'malbut_bringup'))
    from malbut_bringup.fall_setup import prepare_fall_upload
    cli = args_for(tmp_path)
    data = json.loads((Path(__file__).parents[1] / 'config/fall_runtime.example.json').read_text())
    data.update(device_id='robot', journal_path=cli[1])
    config = tmp_path / 'fall.json'
    config.write_text(json.dumps(data))
    args = prepare_fall_upload(config, {
        'HOMECAM_BACKEND_URL': 'https://web.example.com',
        'HOMECAM_DEVICE_TOKEN_FILE': str(tmp_path / 'device.token')})
    monitor, clock, provider, journal, _ = attach(tmp_path)
    try:
        events = scan(monitor, clock, provider, reply(finding()))
        iid = next(e.incident_id for e in events if e.kind == 'incident_opened')
        clock.value += 23
        monitor.flush_people()
        assert journal.pending() and journal.pending_clip() and journal.pending_people()
        received = []

        def server(opener, request, **kwargs):
            payload = json.loads(request.data)
            assert payload['incidentId'] == iid
            assert request.get_header('X-malbut-device-id') == 'robot'
            assert request.get_header('Authorization') == 'Bearer private-test-device-token'
            endpoint = request.full_url.removeprefix('https://web.example.com')
            received.append((endpoint, payload))
            if endpoint == '/api/device/v1/fall-events':
                ack = dict(stored=True, eventId=payload['eventId'])
            else:
                assert endpoint in {'/api/device/v1/fall-incident-clips',
                                    '/api/device/v1/fall-incident-people'}
                ack = dict(stored=True, **{k: payload[k] for k in
                           ('incidentId', 'segmentIndex', 'revision')})
            response = io.BytesIO(json.dumps(ack).encode())
            response.status = 201
            return response

        monkeypatch.setattr(urllib.request.OpenerDirector, 'open', server)

        def stop_when_idle(_):
            raise KeyboardInterrupt

        monkeypatch.setattr(worker.time, 'sleep', stop_when_idle)
        assert worker.main(args) == 0  # Continuous worker drains the backlog then stops in test.
        assert all(r['status'] == 'stored' for r in journal.upload_status())
        assert journal.clip_status()[0]['status'] == journal.people_status()[0]['status'] == 'stored'
        assert [path for path, _ in received] == [
            '/api/device/v1/fall-events'] * 3 + [
            '/api/device/v1/fall-incident-clips', '/api/device/v1/fall-incident-people']
        assert received[1][1]['assessment'] == 'suspected_fall'
        count = len(received)
        assert worker.main(args + ['--once']) == 0
        assert len(received) == count  # Restart does not duplicate acknowledged events.
        assert len(provider.calls) == 1  # Upload never calls a VLM or starts another question.
    finally:
        journal.close()


def test_duplicate_worker_does_not_send_or_change_pending_rows(tmp_path, monkeypatch, capsys):
    args = args_for(tmp_path)
    monitor, clock, provider, journal, _ = attach(tmp_path)
    try:
        scan(monitor, clock, provider, reply(finding()))
        monkeypatch.setattr(worker.HomecamFallEventClient, 'store',
                            lambda *a: pytest.fail('duplicate sender'))
        with worker._upload_lock(Path(args[1])):
            assert worker.main(args + ['--once']) == 0
        assert all(r['status'] == 'pending' for r in journal.upload_status())
        assert 'already running' in capsys.readouterr().out
        # Closing releases the OS lock without deleting the shared lock inode.
        with worker._upload_lock(Path(args[1])):
            pass
    finally:
        journal.close()


@pytest.mark.parametrize('kind', ['symlink', 'public'])
def test_unsafe_worker_lock_is_rejected(tmp_path, kind):
    path = tmp_path / 'events.sqlite'
    lock = tmp_path / 'events.sqlite.upload.lock'
    if kind == 'symlink':
        lock.symlink_to(tmp_path / 'elsewhere')
    else:
        lock.touch(mode=0o644)
        lock.chmod(0o644)
    with pytest.raises((OSError, ValueError)):
        with worker._upload_lock(path):
            pytest.fail('unsafe lock accepted')


def test_upload_failure_keeps_history_pending_without_printing_token(tmp_path, monkeypatch, capsys):
    args = args_for(tmp_path)
    monitor, clock, provider, journal, _ = attach(tmp_path)
    try:
        scan(monitor, clock, provider, reply(finding()))

        def offline(*args, **kwargs):
            raise OSError('private-test-device-token')

        monkeypatch.setattr(urllib.request.OpenerDirector, 'open', offline)
        assert worker.main(args + ['--once']) == 0
        with sqlite3.connect(args[1]) as db:
            rows = db.execute('SELECT status,attempt_count,last_error FROM incident_events').fetchall()
        assert rows[0] == ('pending', 1, 'upload_failed')
        assert len(rows) == 3
        assert 'private-test-device-token' not in capsys.readouterr().out
    finally:
        journal.close()
