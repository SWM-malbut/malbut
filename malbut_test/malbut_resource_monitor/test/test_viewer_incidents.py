"""Read-only evidence APIs: correlation, bounds, original bytes and safe paths."""

import json
from pathlib import Path
import threading
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from malbut_resource_monitor.viewer import LogServer


def write_incident(root, name='incident-100-example', *, uid='u1', text='구독과 좋아요 부탁드려요!',
                   extra_events=0, native_trace=None, shadow_trace=None):
    path = root / name
    path.mkdir(parents=True)
    stamp = 1791728505899334199
    rows = [
        dict(event='job_enqueued', utterance_id=uid, kind='command', generation=1),
        dict(event='result_rejected', utterance_id=uid, reason='stale_generation', generation=0),
        dict(event='transcript_published', utterance_id=uid, text=text),
        dict(event='program_log', utterance_id=uid, logger='malbut_agent_communication',
             message='Agent received ' + uid, source_stamp_ns=stamp,
             payload=dict(event='speech_transcript', status='received', utterance_id=uid)),
        *([dict(event='decode_result', utterance_id=uid, native_decode=native_trace)]
          if native_trace is not None else []),
        *[dict(event='vad_frame', speech=False, rms=0.0) for _ in range(extra_events)],
        *(shadow_trace or []),
    ]
    rows = [dict(monotonic_s=10 + i / 10, wall_ns=stamp+i, **r) for i, r in enumerate(rows)]
    manifest = dict(
        schema_version=1, metadata={'synthetic': True},
        triggers=[dict(utterance_id=uid, text=text, wall_ns=stamp, monotonic_s=10.2)],
        pre_seconds=30, post_seconds=15, post_window_complete=False,
        events=len(rows), audio_saved=False, buffer_evictions_total=2)
    stages = [dict(event=r['event'], event_line=i+1, wall_ns=r['wall_ns'],
                   seconds_from_trigger=r['monotonic_s']-10.2,
                   **{k: r[k] for k in ('text', 'reason', 'native_decode') if k in r})
              for i, r in enumerate(rows) if r.get('utterance_id') == uid]
    trace = dict(transactions=[dict(
        utterance_id=uid, triggered=True, stages=stages,
        observed=dict(stt_published=True, agent_received=True,
                      agent_response_published=False, tts_requested=False, tts_finished=False)),
        dict(utterance_id='non-trigger-uid', triggered=False, stages=[], observed={})])
    for filename, value in [('incident.json', manifest), ('transactions.json', trace)]:
        (path / filename).write_text(json.dumps(value, ensure_ascii=False))
    (path / 'events.jsonl').write_text(''.join(
        json.dumps(r, ensure_ascii=False)+'\n' for r in rows))
    (path / 'rosout.jsonl').write_text(json.dumps(rows[3], ensure_ascii=False)+'\n')
    return path


@pytest.fixture
def viewer(tmp_path):
    root = tmp_path / 'incidents'
    server = LogServer(('127.0.0.1', 0), tmp_path / 'resources', root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'

    def request(route, **params):
        return urlopen(base + route + '?' + urlencode(params))

    try:
        yield root, request, base
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_shadow_frames_and_failures_are_available_in_vad_filter(viewer):
    root, get, _ = viewer
    records = [dict(event=name, observation_only=True)
               for name in ('shadow_vad_started', 'shadow_vad', 'shadow_vad_gap',
                            'shadow_vad_error', 'shadow_vad_stopped')]
    evidence = write_incident(root, extra_events=1, shadow_trace=records)
    with get('/api/incident/events', incident=evidence.name, source='vad') as response:
        data = json.load(response)
    assert data['matching'] == 6
    assert [r['record']['event'] for r in data['rows']] == ['vad_frame'] + [
        r['event'] for r in records]


def test_evidence_is_available_without_resource_sessions_and_preserves_raw_bytes(viewer):
    root, get, _ = viewer
    path = write_incident(root)
    with get('/api/sessions') as response:
        assert json.load(response) == []
    for query in ('구독', 'u1', 'non-trigger-uid'):
        with get('/api/incidents', q=query) as response:
            data = json.load(response)
        assert data['total'] == 1 and data['items'][0]['id'] == path.name
    with get('/api/incident', incident=path.name) as response:
        data = json.load(response)
    assert data['manifest']['audio_saved'] is False
    assert not data['trace']['transactions'][0]['observed']['tts_finished']
    for file in ('incident.json', 'transactions.json', 'events.jsonl', 'rosout.jsonl'):
        with get('/api/incident/download', incident=path.name, file=file) as response:
            assert response.read() == (path / file).read_bytes()
    with get('/incident-viewer.js') as response:
        assert 'javascript' in response.headers['Content-Type']
        assert '원본 필드 보기' in response.read().decode()


def test_default_stt_capture_is_visible_when_viewer_opens_later(tmp_path, monkeypatch):
    # Exercise the real recorder and HTTP store across packages, with no ROS,
    # microphone, model, environment setting or custom incident-root argument.
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / 'malbut_stt'))
    from malbut_stt import incident
    from malbut_resource_monitor import viewer as viewer_module

    assert incident.DEFAULT_DIRECTORY == viewer_module.DEFAULT_LOG_ROOT + '/stt_incidents'
    root = tmp_path / 'resource_logs'
    monkeypatch.setattr(incident, 'DEFAULT_DIRECTORY', str(root / 'stt_incidents'))
    recorder = incident.from_environment({})
    try:
        recorder.audio(bytes(640), blocked=False)
        recorder.event('job_enqueued', utterance_id='automatic', kind='command')
        recorder.event('transcript_published', utterance_id='automatic', text='구독과 좋아요!')
    finally:
        recorder.close()
    assert not list(root.rglob('*.wav'))
    assert recorder.last_incident.is_dir()
    with LogServer(('127.0.0.1', 0), root) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f'http://127.0.0.1:{server.server_port}'
            with urlopen(base + '/api/incidents') as response:
                data = json.load(response)
            assert data['total'] == 1
            assert data['items'][0]['utterance_ids'] == ['automatic']
            assert data['root'] == str(root / 'stt_incidents')
            with urlopen(base + '/api/incident?' + urlencode(
                    {'incident': data['items'][0]['id']})) as response:
                detail = json.load(response)
            assert detail['manifest']['audio_saved'] is False
            assert detail['trace']['transactions'][0]['observed']['stt_published']
            with urlopen(base + '/api/sessions') as response:
                assert json.load(response) == [], 'incident storage is not a resource session'
        finally:
            server.shutdown()
            thread.join()


def test_default_viewer_reads_legacy_and_new_evidence_without_moving_files(tmp_path, monkeypatch):
    from malbut_resource_monitor import viewer as viewer_module

    root, legacy = tmp_path / 'resource_logs', tmp_path / 'stt_incidents'
    monkeypatch.setattr(viewer_module, 'DEFAULT_LOG_ROOT', str(root))
    monkeypatch.setattr(viewer_module, 'LEGACY_INCIDENT_ROOT', str(legacy))
    old = write_incident(legacy, 'incident-100-old', text='이전 기록')
    new = write_incident(root / 'stt_incidents', 'incident-200-new', text='새 기록')
    snapshot = {p: p.read_bytes() for path in (old, new) for p in path.iterdir()}
    with LogServer(('127.0.0.1', 0), root) as server:
        data = server.incidents.list()
        assert data['total'] == 2
        assert data['legacy_roots'] == [str(legacy)]
        assert server.incidents.detail(old.name)['manifest']['triggers'][0]['text'] == '이전 기록'
        assert server.incidents.detail(new.name)['manifest']['triggers'][0]['text'] == '새 기록'
    assert all(path.read_bytes() == raw for path, raw in snapshot.items())
    with LogServer(('127.0.0.1', 0), root, root / 'stt_incidents') as server:
        assert server.incidents.list()['total'] == 1, 'explicit roots must remain isolated'


def test_event_filters_pagination_and_damage_keep_original_line_numbers(viewer):
    root, get, _ = viewer
    path = write_incident(root, extra_events=205)
    with (path / 'events.jsonl').open('ab') as stream:
        stream.write(b'broken\n[]\n{"event":"bad","value":NaN}\n')
        stream.write(b'x' * 70000 + b'\n')
        stream.write(b'{"event":"last"}\n{"event":"incomplete"')
    with get('/api/incident/events', incident=path.name, source='vad') as response:
        first = json.load(response)
    assert first['matching'] == 205 and len(first['rows']) == 200 and first['has_more']
    assert first['rows'][0]['line'] == 5 and first['malformed'] == 4
    assert first['partial_line']
    with get('/api/incident/events', incident=path.name, source='vad', offset=200) as response:
        second = json.load(response)
    assert len(second['rows']) == 5 and second['rows'][0]['line'] == 205
    assert not second['has_more']
    with get('/api/incident/events', incident=path.name, source='program', q='Agent') as response:
        row = json.load(response)['rows'][0]
        assert row['line'] == 4
        assert row['raw'] == (path / 'events.jsonl').read_text().splitlines()[3]
    with get('/api/incident/events', incident=path.name, q='stale_generation') as response:
        assert json.load(response)['rows'][0]['line'] == 2


def test_only_complete_valid_bundles_appear_and_empty_root_is_supported(viewer):
    root, get, _ = viewer
    with get('/api/incidents') as response:
        assert json.load(response)['items'] == []
    write_incident(root)
    write_incident(root, '.partial-incident-pending')
    broken = write_incident(root, 'incident-broken')
    (broken / 'transactions.json').write_text('{"transactions":[{}]}')
    with get('/api/incidents') as response:
        data = json.load(response)
    assert data['total'] == 1 and data['unreadable'] == 1


def test_new_bundle_invalidates_list_cache_and_query_is_part_of_etag(viewer):
    root, get, base = viewer
    write_incident(root)
    with get('/api/incidents') as response:
        etag = response.headers['ETag']
    with pytest.raises(HTTPError) as caught:
        urlopen(Request(base + '/api/incidents?', headers={'If-None-Match': etag}))
    assert caught.value.code == 304
    write_incident(root, 'incident-200-new', uid='u2')
    with urlopen(Request(base + '/api/incidents?', headers={'If-None-Match': etag})) as response:
        assert json.load(response)['total'] == 2
    with get('/api/incident/events', incident='incident-200-new', source='all') as response:
        etag = response.headers['ETag']
    path = '/api/incident/events?' + urlencode(dict(incident='incident-200-new', source='program'))
    with urlopen(Request(base + path, headers={'If-None-Match': etag})) as response:
        assert json.load(response)['matching'] == 1


@pytest.mark.parametrize('route,params', [
    ('/api/incident', {'incident': '../secret'}),
    ('/api/incident', {'incident': '.partial-incident-hidden'}),
    ('/api/incident/download', {'file': '../secret'}),
    ('/api/incident/download', {'file': 'microphone.wav'}),
    ('/api/incident/events', {'offset': '-1'}),
    ('/api/incident/events', {'limit': '201'}),
    ('/api/incident/events', {'source': 'unknown'}),
])
def test_invalid_requests_are_rejected(viewer, route, params):
    root, get, _ = viewer
    path = write_incident(root)
    with pytest.raises(HTTPError) as caught:
        get(route, **dict({'incident': path.name}, **params))
    assert caught.value.code == 400


def test_symlinked_directories_and_evidence_cannot_escape_root(viewer, tmp_path):
    root, get, _ = viewer
    outside = write_incident(tmp_path / 'outside')
    root.mkdir()
    (root / 'incident-linked').symlink_to(outside, target_is_directory=True)
    path = write_incident(root)
    (path / 'events.jsonl').unlink()
    (path / 'events.jsonl').symlink_to(outside / 'events.jsonl')
    for route, params in [('/api/incident', {'incident': 'incident-linked'}),
                          ('/api/incident/events', {'incident': path.name})]:
        with pytest.raises(HTTPError) as caught:
            get(route, **params)
        assert caught.value.code == 400
