"""Triggered evidence must be bounded, correlated, and observational."""

import hashlib
import json
import os
from queue import Full
from threading import Event, Thread
from types import SimpleNamespace
import wave

import numpy as np
import pytest

from malbut_stt.dialogue_pipeline import DialoguePipeline
from malbut_stt.incident import IncidentRecorder, from_environment
from malbut_stt.incident_cli import main


PHRASE = '구독과 좋아요 부탁드려요!'


def read_events(path):
    return [json.loads(line) for line in (path / 'events.jsonl').read_text().splitlines()]


def read_pcm(path):
    with wave.open(str(path), 'rb') as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        return wav.readframes(wav.getnframes())


@pytest.fixture
def capture(tmp_path):
    recorders = []

    def create(**options):
        recorder = IncidentRecorder(tmp_path / 'incidents', **options)
        recorders.append(recorder)
        return recorder

    yield create
    for recorder in recorders:
        recorder.close()


def trigger(recorder, uid='u1', text=PHRASE):
    recorder.event('transcript_published', utterance_id=uid, text=text, session_id='')


def test_snapshot_contains_lossless_model_input_and_before_after_events(capture):
    r = capture(post_seconds=60, metadata={'backend': 'test-model'})
    pcm = np.arange(-32768, 32768, dtype='<i2').tobytes()
    audio = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
    segment = SimpleNamespace(text=PHRASE, start=0.0, end=2.0, no_speech_prob=0.95)
    info = object()
    calls = []

    class Model:
        def transcribe(self, value, **options):
            calls.append((value, options))
            return iter([segment]), info

    r.audio(pcm, captured_at=10, blocked=True, busy=False)
    r.event('wake_detected')
    with r.context(kind='command', utterance_id='u1', generation=3):
        segments, actual_info = r.wrap_model(Model()).transcribe(audio, initial_prompt=None)
    assert actual_info is info and calls[0][0] is audio
    assert not r.directory.exists()
    assert list(segments) == [segment]
    trigger(r)
    r.event('tts_request', request_id='u1', playback_id='p1', text='응답')
    r.event('tts_playback', request_id='u1', playback_id='p1', state='finished')
    r.close()
    assert r.error is None
    path = r.last_incident
    manifest = json.loads((path / 'incident.json').read_text())
    assert manifest['triggers'][0]['utterance_id'] == 'u1'
    assert manifest['metadata'] == {'backend': 'test-model'}
    assert not manifest['post_window_complete']
    events = read_events(path)
    assert [x['event'] for x in events] == [
        'microphone_chunk', 'wake_detected', 'decode_input', 'decode_result',
        'transcript_published', 'tts_request', 'tts_playback']
    assert read_pcm(path / 'microphone.wav') == pcm
    decoded, = (path / 'decodes').glob('*.json')
    record = json.loads(decoded.read_text())
    assert record['segments'] == [vars(segment)]
    assert record['context'] == {'kind': 'command', 'utterance_id': 'u1', 'generation': 3}
    assert record['input_available'] and record['result_available']
    assert read_pcm(path / record['wav']) == pcm
    assert record['pcm_sha256'] == hashlib.sha256(pcm).hexdigest()
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in path.rglob('*') if p.is_file())


@pytest.mark.parametrize('event,text,matched', [
    ('transcript_published', '구 독 / 좋 아 요!', True),
    ('transcript_published', ' 감사합니다. ', True),
    ('transcript_published', '말씀 감사합니다. 그런데 오늘 날씨는?', False),
    ('transcript_published', '좋아요. 내일 만나요.', False),
    ('inference_result', PHRASE, False),
    ('tts_request', PHRASE, False),
    ('recognition_rejected', PHRASE, True),
    ('recognition_rejected', '지금 거실로 와 주세요.', False),
])
def test_matching_publications_or_quality_rejections_trigger(capture, event, text, matched):
    r = capture()
    r.event(event, utterance_id='u1', text=text)
    r.close()
    assert (r.last_incident is not None) is matched
    if not matched:
        assert not r.directory.exists()
    else:
        manifest = json.loads((r.last_incident / 'incident.json').read_text())
        assert manifest['triggers'][0]['event'] == event


def test_repeated_uid_is_one_trigger_and_nearby_utterances_share_window(capture):
    r = capture(post_seconds=60)
    trigger(r)
    trigger(r)
    trigger(r, 'u2')
    r.close()
    manifest = json.loads((r.last_incident / 'incident.json').read_text())
    assert [x['utterance_id'] for x in manifest['triggers']] == ['u1', 'u2']


def test_window_expires_without_more_audio_or_ros_callbacks(capture):
    saved = Event()
    r = capture(post_seconds=0.02, on_saved=lambda path: saved.set())
    trigger(r)
    assert saved.wait(2), 'idle recorder must finalize its post-trigger window'
    manifest = json.loads((r.last_incident / 'incident.json').read_text())
    assert manifest['post_window_complete']


def test_time_eviction_and_byte_and_count_caps(capture):
    now = [0.0]
    r = capture(pre_seconds=2, post_seconds=60, clock=lambda: now[0],
                max_buffer_bytes=3000, max_entries=3)
    r.event('old')
    now[0] = 5
    for i in range(30):
        r.audio(bytes(640), sequence=i)
    assert r._buffer_bytes <= 3000 and len(r._entries) <= 3
    assert r._evictions > 0
    trigger(r)
    r.close()
    assert 'old' not in [e['event'] for e in read_events(r.last_incident)]
    manifest = json.loads((r.last_incident / 'incident.json').read_text())
    assert manifest['buffer_evictions_total'] > 0


def test_retains_pre_trigger_window_through_post_window(capture):
    now = [0.0]
    r = capture(pre_seconds=5, post_seconds=20, clock=lambda: now[0])
    r.event('too_old')
    now[0] = 8
    r.event('wake_detected')
    now[0] = 10
    trigger(r)
    now[0] = 25
    r.event('tts_playback', state='finished')
    r.close()
    assert [e['event'] for e in read_events(r.last_incident)] == [
        'wake_detected', 'transcript_published', 'tts_playback']


def test_slow_disk_does_not_hold_the_capture_lock(capture, monkeypatch):
    entered, release = Event(), Event()
    r = capture(post_seconds=0)
    original = r._save

    def delayed(*args):
        entered.set()
        assert release.wait(3)
        original(*args)

    monkeypatch.setattr(r, '_save', delayed)
    trigger(r)
    assert entered.wait(2)
    try:
        # This call would deadlock if the writer held the buffer condition.
        r.audio(bytes(640))
        assert r._entries[-1].pcm == bytes(640)
    finally:
        release.set()
    r.close()
    assert r.last_incident is not None


def test_disk_quota_counts_previous_evidence_and_does_not_delete_it(capture):
    r = capture(max_bytes=1)
    r.directory.mkdir()
    previous = r.directory / 'keep.txt'
    previous.write_text('existing evidence')
    trigger(r)
    r.close()
    assert 'quota' in r.error
    assert previous.read_text() == 'existing evidence'
    assert not list(r.directory.glob('incident-*'))


def test_late_trigger_during_slow_write_starts_a_separate_window(capture, monkeypatch):
    entered, release = Event(), Event()
    now = [0.0]
    r = capture(post_seconds=0, clock=lambda: now[0])
    original = r._save

    def delayed(*args):
        entered.set()
        assert release.wait(3)
        original(*args)

    monkeypatch.setattr(r, '_save', delayed)
    trigger(r, 'first')
    assert entered.wait(2)
    try:
        now[0] = 10
        trigger(r, 'second')
        now[0] = 20
        trigger(r, 'third')
        assert len(r._ready) == 1
        now[0] = 30
        trigger(r, 'fourth')
        assert r._skipped_triggers == 1, 'backlog must be bounded without blocking capture'
    finally:
        release.set()
    r.close()
    bundles = [json.loads(p.read_text()) for p in r.directory.glob('incident-*/incident.json')]
    assert sorted(b['triggers'][0]['utterance_id'] for b in bundles) == [
        'first', 'second', 'third']
    assert all(len(b['triggers']) == 1 for b in bundles)


def test_writer_error_leaves_no_completed_bundle_and_does_not_raise(capture, monkeypatch):
    errors = []
    r = capture(on_error=errors.append)

    def full(*args):
        raise OSError('disk full')

    monkeypatch.setattr(r, '_write', full)
    trigger(r)
    r.close()
    assert 'disk full' in r.error
    assert r.last_incident is None
    assert not list(r.directory.glob('incident-*'))
    assert errors == ['OSError: disk full']


def test_vad_decision_and_model_exception_are_unchanged(capture):
    r = capture()
    frame = np.full(320, 3, dtype='<i2').tobytes()
    assert r.wrap_vad(lambda f, rate: False, kind='wake')(frame, 16000) is False
    error = RuntimeError('decode failed')

    class Model:
        def transcribe(self, audio, **kwargs):
            def segments():
                yield SimpleNamespace(text='partial', start=0, end=1)
                raise error
            return segments(), None

    segments, _ = r.wrap_model(Model()).transcribe(np.zeros(320, dtype=np.float32))
    with pytest.raises(RuntimeError) as caught:
        list(segments)
    assert caught.value is error
    trigger(r)
    r.close()
    events = read_events(r.last_incident)
    assert events[0]['speech'] is False and events[0]['kind'] == 'wake'
    result = next(e for e in events if e['event'] == 'decode_result')
    assert result['error']['type'] == 'RuntimeError' and len(result['segments']) == 1


def test_pipeline_publishes_original_text_and_captures_actual_vad(capture):
    r = capture()
    published = []
    voice = b'\x01\x00' * 320

    def publish(uid, text):
        published.append((uid, text))
        r.event('transcript_published', utterance_id=uid, text=text)

    pipeline = DialoguePipeline(
        recorder_factory=lambda: None, wake=SimpleNamespace(), transcriber=SimpleNamespace(),
        is_speech=lambda f, rate: f == voice, diagnostics=r,
        publish_transcript=publish,
        publish_control=lambda *args: None, publish_interruption=lambda *args: None,
        report=lambda event: None)
    try:
        pipeline.session.activate()
        pipeline.feed(voice * 4)
        uid = pipeline.session.utterance_id
        pipeline._accept_result('command', pipeline._generation, uid, PHRASE, None)
        assert published == [(uid, PHRASE)]
        assert pipeline._reply_request_id == uid
    finally:
        pipeline.close()
        r.close()
    events = read_events(r.last_incident)
    assert any(e['event'] == 'vad_frame' and e['kind'] == 'command' for e in events)
    assert events[-1]['event'] == 'transcript_published'
    assert events[-1]['utterance_id'] == uid


def test_microphone_evidence_is_captured_before_playback_gating(capture):
    r = capture()
    pipeline = DialoguePipeline(
        recorder_factory=lambda: None, wake=SimpleNamespace(), transcriber=SimpleNamespace(),
        is_speech=lambda f, rate: False, diagnostics=r,
        publish_transcript=lambda *args: None, publish_control=lambda *args: None,
        publish_interruption=lambda *args: None, report=lambda event: None)
    reads = []

    def read():
        if reads:
            pipeline.stopping.set()
        reads.append(True)
        return [123] * 512

    pipeline.recorder = SimpleNamespace(read=read, delete=lambda: None)
    pipeline._input_blocked = lambda now: True
    try:
        pipeline._capture()
        assert pipeline.audio.empty(), 'gated audio must not reach inference'
        trigger(r)
    finally:
        pipeline.close()
        r.close()
    assert read_pcm(r.last_incident / 'microphone.wav') == np.full(512, 123, dtype='<i2').tobytes()
    mic, = [e for e in read_events(r.last_incident) if e['event'] == 'microphone_chunk']
    assert mic['blocked'] is True


def test_demo_runs_without_ros_or_models(tmp_path):
    output = tmp_path / 'demo'
    assert main(['--demo', '--output', str(output)]) == 0
    path, = output.glob('incident-*')
    manifest = json.loads((path / 'incident.json').read_text())
    assert manifest['metadata']['synthetic'] is True
    assert read_pcm(path / 'microphone.wav')


def test_cli_forwards_ros_args_and_restores_environment(tmp_path, monkeypatch):
    import malbut_stt.node

    monkeypatch.setenv('MALBUT_STT_INCIDENT_CONFIG', 'previous')
    monkeypatch.delenv('MALBUT_STT_DIAGNOSTIC_DIR', raising=False)
    seen = []

    def run(args):
        seen.append((args, json.loads(os.environ['MALBUT_STT_INCIDENT_CONFIG'])))
        return 17

    monkeypatch.setattr(malbut_stt.node, 'main', run)
    assert main(['--output', str(tmp_path), '--no-audio', '--no-default-triggers',
                 '--exact', '테스트',
                 '--', '--ros-args', '-p', 'backend:=whisper_cpp']) == 17
    assert seen[0][0] == ['--ros-args', '-p', 'backend:=whisper_cpp']
    assert seen[0][1]['contains'] == [] and seen[0][1]['exact'] == ['테스트']
    assert seen[0][1]['save_audio'] is False
    assert os.environ['MALBUT_STT_INCIDENT_CONFIG'] == 'previous'


def test_mutually_exclusive_modes_fail_before_creating_a_recorder(tmp_path):
    with pytest.raises(ValueError, match='either'):
        from_environment({'MALBUT_STT_INCIDENT_CONFIG': json.dumps({'directory': str(tmp_path)}),
                          'MALBUT_STT_DIAGNOSTIC_DIR': str(tmp_path)})


def test_automatic_capture_defaults_to_logs_and_continuous_mode_can_replace_it():
    assert from_environment({'MALBUT_STT_DIAGNOSTIC_DIR': '/unused'}) is None
    assert from_environment({'MALBUT_STT_INCIDENT_CONFIG': '{"enabled":false}'}) is None
    for enabled in ('false', 0, None):
        with pytest.raises(ValueError, match='enabled'):
            from_environment({'MALBUT_STT_INCIDENT_CONFIG': json.dumps({'enabled': enabled})})
    recorder = from_environment({})
    try:
        assert recorder.save_audio is False
        assert str(recorder.directory).endswith('/.ros/malbut/resource_logs/stt_incidents')
        assert recorder.max_bytes == 512 * 1024 * 1024
    finally:
        recorder.close()


@pytest.mark.parametrize('options', [
    {'pre_seconds': -1}, {'post_seconds': float('inf')}, {'max_buffer_bytes': 0},
    {'contains': ['']}, {'contains': [], 'exact': []}, {'max_entries': True},
    {'save_audio': 'false'},
])
def test_invalid_configuration_is_rejected(capture, options):
    with pytest.raises(ValueError):
        capture(**options)


def log_message(payload, *, logger='malbut_agent_communication'):
    return SimpleNamespace(
        name=logger, level=20, msg=json.dumps(payload, ensure_ascii=False),
        stamp=SimpleNamespace(sec=10, nanosec=3), file='source.py', function='receive', line=35)


def test_log_only_keeps_decode_and_program_evidence_without_retaining_pcm(capture):
    r = capture(save_audio=False)
    pcm = np.arange(640, dtype='<i2').tobytes()

    class Model:
        def transcribe(self, audio, **options):
            return iter([SimpleNamespace(text=PHRASE, start=0.0, end=0.04)]), None

    r.audio(pcm)
    with r.context(utterance_id='u1', kind='command'):
        segments, _ = r.wrap_model(Model()).transcribe(
            np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768, language='ko')
        assert list(segments)[0].text == PHRASE
    trigger(r)
    r.ros_log(log_message({'event': 'speech_transcript', 'utterance_id': 'u1',
                          'status': 'received'}))
    r.ros_log(log_message({'event': 'dialogue_response_published', 'utterance_id': 'u1',
                          'text': '답변'}))
    r.event('tts_request', request_id='u1', playback_id='p1', text='답변')
    r.event('tts_playback', playback_id='p1', state='finished')
    assert all(not e.pcm for e in r._entries), 'log-only mode must not retain PCM in its ring'
    r.close()
    assert not list(r.last_incident.rglob('*.wav'))
    manifest = json.loads((r.last_incident / 'incident.json').read_text())
    assert not manifest['audio_saved']
    decode, = r.last_incident.glob('decodes/*.json')
    record = json.loads(decode.read_text())
    assert record['input_observed'] and not record['input_available'] and record['wav'] is None
    assert record['segments'][0]['text'] == PHRASE
    document = json.loads((r.last_incident / 'transactions.json').read_text())
    transaction, = document['transactions']
    assert transaction['utterance_id'] == 'u1' and all(transaction['observed'].values())
    events = read_events(r.last_incident)
    for stage in transaction['stages']:
        assert events[stage['event_line'] - 1]['event'] == stage['event']
    logs = (r.last_incident / 'rosout.jsonl').read_text().splitlines()
    assert len(logs) == 2 and json.loads(logs[0])['source_stamp_ns'] == 10000000003


def test_program_log_filter_and_missing_receipt_do_not_claim_agent_success(capture):
    r = capture(save_audio=False)
    trigger(r)
    r.ros_log(log_message({'event': 'speech_transcript', 'status': 'received',
                          'utterance_id': 'u1'}, logger='unrelated_node'))
    r.ros_log(log_message({'event': 'speech_transcript', 'status': 'received',
                          'utterance_id': 'different_turn'}))
    r.ros_log(log_message({'event': 'speech_transcript', 'status': 'duplicate',
                          'utterance_id': 'u1'}))
    r.ros_log(log_message('speech_transcript storage_error: receipt not confirmed'))
    long_log = log_message('unused')
    long_log.msg = '긴 로그' * 20000
    r.ros_log(long_log)
    r.close()
    assert r.error is None
    document = json.loads((r.last_incident / 'transactions.json').read_text())
    primary, other = document['transactions']
    assert primary['utterance_id'] == 'u1' and not primary['observed']['agent_received']
    assert other['utterance_id'] == 'different_turn' and other['observed']['agent_received']
    logs = [json.loads(s) for s in (r.last_incident / 'rosout.jsonl').read_text().splitlines()]
    assert len(logs) == 4 and logs[-1]['truncated']
    assert logs[-2]['payload'] is None and logs[-2]['utterance_id'] is None


def trace_pipeline(recorder):
    return DialoguePipeline(
        recorder_factory=lambda: None, wake=SimpleNamespace(),
        transcriber=SimpleNamespace(transcribe=lambda pcm, rate: PHRASE),
        is_speech=lambda f, rate: True, diagnostics=recorder,
        publish_transcript=lambda uid, text: recorder.event(
            'transcript_published', utterance_id=uid, text=text),
        publish_control=lambda *args: None, publish_interruption=lambda *args: None,
        report=lambda event: None)


def test_real_worker_queue_path_links_result_adoption_to_final_publication(capture, monkeypatch):
    r = capture(save_audio=False)
    pipeline = trace_pipeline(r)
    result_ready = Event()
    original = r.event

    def observe(name, **metadata):
        original(name, **metadata)
        if name == 'result_enqueued':
            result_ready.set()

    monkeypatch.setattr(r, 'event', observe)
    try:
        pipeline.session.activate()
        pipeline.feed(bytes(640) * 4)
        uid = pipeline.session.utterance_id
        pipeline._complete_capture('command', SimpleNamespace(
            revision=1, pcm=bytes(32000 * 3), silence_s=2.0, audio_start_s=0.0,
            voiced_s=1.0))
        pipeline.asr_thread = Thread(target=pipeline._infer)
        pipeline.asr_thread.start()
        assert result_ready.wait(2)
        pipeline.poll()
    finally:
        pipeline.close()
        r.close()
    document = json.loads((r.last_incident / 'transactions.json').read_text())
    transaction, = document['transactions']
    names = [e['event'] for e in transaction['stages']]
    expected = ['job_enqueued', 'job_dequeued', 'inference_result', 'result_enqueued',
                'result_dequeued', 'result_accepted', 'transcript_published']
    assert [n for n in names if n in expected] == expected
    assert transaction['utterance_id'] == uid and not transaction['observed']['agent_received']


@pytest.mark.parametrize('case,reason', [
    ('generation', 'stale_generation'), ('uid', 'inactive_or_different_utterance'),
    ('empty', 'empty_transcript'), ('error', 'transcription_failed:RuntimeError'),
])
def test_rejected_result_preserves_reason_and_does_not_publish(capture, case, reason):
    r = capture(save_audio=False)
    pipeline = trace_pipeline(r)
    try:
        pipeline.session.activate()
        pipeline.feed(bytes(640) * 4)
        uid = pipeline.session.utterance_id
        pipeline._accept_result(
            'command', -1 if case == 'generation' else pipeline._generation,
            'stale-uid' if case == 'uid' else uid, '' if case == 'empty' else PHRASE,
            'RuntimeError' if case == 'error' else None)
        assert all(json.loads(e.record)['event'] != 'transcript_published' for e in r._entries)
        trigger(r, uid)
    finally:
        pipeline.close()
        r.close()
    rejected, = [e for e in read_events(r.last_incident) if e['event'] == 'result_rejected']
    assert rejected['reason'] == reason


@pytest.mark.parametrize('cached', [True, False])
def test_endpoint_reuse_has_an_explicit_link_to_the_final_result(capture, cached):
    r = capture(save_audio=False)
    pipeline = trace_pipeline(r)
    try:
        pipeline.session.activate()
        pipeline.feed(bytes(640) * 4)
        uid = pipeline.session.utterance_id
        key = (pipeline._generation, uid, 1)
        if cached:
            pipeline._endpoint_result = (key, PHRASE, None)
            pipeline._complete_capture('command', SimpleNamespace(
                revision=1, pcm=bytes(640), silence_s=2.0, audio_start_s=0.0))
        else:
            pipeline._endpoint_final = key
            pipeline._final_input = bytes(640)
            pipeline._accept_endpoint(key[0], key[1:], PHRASE, None)
    finally:
        pipeline.close()
        r.close()
    reused, = [e for e in read_events(r.last_incident) if e['event'] == 'endpoint_reused']
    assert reused['utterance_id'] == uid and reused['revision'] == 1
    assert reused['reason'] == ('cached_result' if cached else 'final_capture_waiting')


def test_queue_rejection_preserves_original_job_and_exception(capture):
    r = capture(save_audio=False)
    pipeline = trace_pipeline(r)
    job = ('command', 0, 'u1', bytes(640))
    try:
        pipeline._enqueue_job(job)
        with pytest.raises(Full):
            pipeline._enqueue_job(('command', 0, 'u2', b''))
        assert pipeline.jobs.get_nowait() == job
        trigger(r)
    finally:
        pipeline.close()
        r.close()
    rejected, = [e for e in read_events(r.last_incident) if e['event'] == 'job_rejected']
    assert rejected['utterance_id'] == 'u2' and rejected['reason'] == 'jobs_queue_full'
