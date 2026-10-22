"""Exercise durable visual evidence, retry recovery, owner isolation and dialogue."""

import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from malbut_agent_server.config import Settings
from malbut_agent_server.factory import build_orchestrator
from malbut_agent_server.patrol_memory import PatrolMemoryStore, read_capture
from malbut_agent_server.patrol_observer import OpenAIPatrolObserver, PatrolObservationWorker
from malbut_agent_server.patrol_query import configure_patrol_queries
from malbut_agent_server.providers.openai_responses import OpenAIResponsesProvider
from malbut_agent_server.schemas import AgentDecision, AgentRequest, ProviderResult, RobotState
from malbut_agent_server.service_keys import ManagedKey


ANALYSIS = {'summary': '소파 위에 리모컨이 보인다.', 'objects': [
    {'name': '리모컨', 'location': '소파 위', 'confidence': 'high'}], 'uncertainties': []}
ARGS = {'query': '리모컨', 'since': None, 'until': None, 'limit': 5}


def capture(directory, *, simulated=False, captured_at='2026-10-21T05:00:00+00:00'):
    folder = directory / 'captures'
    folder.mkdir(exist_ok=True)
    identifier = uuid4().hex
    image = b'\xff\xd8offline-transport-fixture\xff\xd9'
    metadata = {
        'version': 1, 'observation_id': identifier, 'patrol_id': uuid4().hex,
        'map_id': 'a' * 64, 'map_frame': 'map', 'room_map': 'home.geojson',
        'camera_frame': 'rgb_optical', 'captured_at': captured_at,
        'image_stamp': {'sec': 1, 'nanosec': 0}, 'simulated': simulated,
        'camera_pose': {'x': 1.0, 'y': 2.0, 'yaw': 0.0}, 'capture_rooms': ['거실'],
        'image_sha256': hashlib.sha256(image).hexdigest(),
    }
    path = folder / (identifier + '.json')
    path.with_suffix('.jpg').write_bytes(image)
    path.write_text(json.dumps(metadata, ensure_ascii=False))
    return path, metadata


@pytest.fixture
def evidence(tmp_path):
    store = PatrolMemoryStore(tmp_path, 'owner')
    path, metadata = capture(tmp_path)
    observer = SimpleNamespace(available=lambda: True, model='fixture',
                               analyze=lambda _metadata, _image: copy.deepcopy(ANALYSIS))
    worker = PatrolObservationWorker(store, observer, autostart=False)
    return store, worker, path, metadata


def test_photo_survives_restart_and_queries_keep_time_location_and_source(evidence):
    store, worker, path, metadata = evidence
    assert worker.run_once()
    assert not worker.run_once()  # No repeated paid analysis after completion.
    reopened = PatrolMemoryStore(store.directory, 'owner')
    result = reopened.search('owner', ARGS)
    item, = result['observations']
    assert item['captured_at'] == metadata['captured_at']
    assert item['capture_rooms'] == ['거실']
    assert item['photo'] == 'captures/' + path.stem + '.jpg'
    original = {k: v for k, v in item['objects'][0].items() if k != 'spatial'}
    assert original == ANALYSIS['objects'][0]
    assert item['objects'][0]['spatial']['reason'] == 'legacy_without_depth'
    assert result['counts'] == {'ready': 1}
    assert not reopened.search('owner', dict(ARGS, query='안경'))['observations']
    later = reopened.search('owner', dict(ARGS, since='2026-10-21T15:00:00+09:00'))
    assert not later['observations']
    assert reopened.search('owner', dict(ARGS, since='2026-10-21T13:00:00+09:00'))['observations']
    with pytest.raises(ValueError):
        reopened.search('owner', dict(ARGS, since='2026-10-21T13:00:00'))
    with pytest.raises(PermissionError):
        reopened.search('other-owner', ARGS)
    with pytest.raises(ValueError, match='another robot owner'):
        PatrolMemoryStore(store.directory, 'other-owner')


@pytest.mark.parametrize('latest_located', [True, False])
def test_moved_object_keeps_history_and_uses_capture_time_not_analysis_order(
        tmp_path, latest_located):
    """A late analysis of an old photo cannot replace a newer position or uncertainty."""
    store = PatrolMemoryStore(tmp_path, 'owner')
    newer_time, older_time = '2026-10-21T18:30:00+09:00', '2026-10-21T09:00:00+00:00'
    newer, _ = capture(tmp_path, captured_at=newer_time)
    older, _ = capture(tmp_path, captured_at=older_time)
    store.ingest()

    def observation(room, *, located=True):
        return {'summary': '붉은 상자가 보인다.', 'objects': [{
            'name': '붉은 상자', 'location': '사진 가운데', 'confidence': 'high',
            'spatial': {'status': 'located' if located else 'unknown',
                        'region': {'id': room, 'name': room} if located else None,
                        'map_position': None, 'method': 'registered_rgbd',
                        'reason': None if located else 'depth_missing'}}], 'uncertainties': []}

    # New photo is analyzed first; the older workroom image finishes afterward.
    new_analysis = observation('거실', located=latest_located)
    old_analysis = observation('작업실')
    store.complete(newer.stem, new_analysis, 'fixture')
    store.complete(older.stem, old_analysis, 'fixture')
    reopened = PatrolMemoryStore(tmp_path, 'owner')
    arguments = dict(ARGS, query='붉은', limit=5)
    result = reopened.search('owner', arguments)
    new, old = result['observations']
    assert [new['observation_id'], old['observation_id']] == [newer.stem, older.stem]
    assert result['ordering'] == 'captured_at_desc'
    assert result['object_identity'] == 'untracked_visual_descriptions'
    assert new['objects'] == new_analysis['objects']
    assert old['objects'] == old_analysis['objects']
    assert new['patrol_id'] != old['patrol_id']
    # A one-hit latest request still returns unknown rather than skipping to old certainty.
    latest, = reopened.search('owner', dict(arguments, limit=1))['observations']
    assert latest['observation_id'] == newer.stem
    historical, = reopened.search('owner', dict(
        arguments, until=newer_time))['observations']
    assert historical['observation_id'] == older.stem
    with reopened.connect() as connection:
        saved = connection.execute('SELECT analysis FROM observations WHERE id=?',
                                   (older.stem,)).fetchone()[0]
        assert json.loads(saved) == old_analysis


def test_recent_context_catches_changed_descriptions_without_claiming_keyword_match(tmp_path):
    """Different visual wording cannot hide a newer observation behind an exact old hit."""
    store = PatrolMemoryStore(tmp_path, 'owner')
    before, _ = capture(tmp_path, captured_at='2026-10-21T09:00:00+00:00')
    after, _ = capture(tmp_path, captured_at='2026-10-21T09:30:00+00:00')
    store.ingest()
    for path, name in [(before, '붉은 직사각형 물체'), (after, '빨간색 직육면체')]:
        store.complete(path.stem, {'summary': name + '가 보인다.', 'objects': [
            {'name': name, 'location': '중앙', 'confidence': 'high'}],
            'uncertainties': []}, 'fixture')
    options = dict(ARGS, query='붉은')
    result = store.search('owner', options, include_recent_context=True)
    assert [r['observation_id'] for r in result['observations']] == [after.stem, before.stem]
    assert [r['match_kind'] for r in result['observations']] == ['recent_context', 'keyword']
    assert len(json.dumps(result, ensure_ascii=False)) <= 4000
    latest, = store.search(
        'owner', dict(options, limit=1), include_recent_context=True)['observations']
    assert latest['observation_id'] == after.stem
    # No literal match is required, but these remain explicitly unconfirmed candidates.
    for query in ('빨간 상자', '초록색 상자'):
        result = store.search('owner', dict(options, query=query), include_recent_context=True)
        assert len(result['observations']) == 2
        assert all(r['match_kind'] == 'recent_context' for r in result['observations'])
    earlier, = store.search(
        'owner', dict(options, query='빨간 상자', until='2026-10-21T09:20:00+00:00'),
        include_recent_context=True)['observations']
    assert earlier['observation_id'] == before.stem
    with pytest.raises(PermissionError):
        store.search('other', options, include_recent_context=True)
    # Even fallback candidates must retain valid photo provenance.
    after.with_suffix('.jpg').unlink()
    result = store.search('owner', dict(options, query='상자'), include_recent_context=True)
    assert [r['observation_id'] for r in result['observations']] == [before.stem]


def test_missing_key_keeps_photo_pending_then_recovers_with_existing_key(evidence):
    store, worker, path, _ = evidence
    worker.observer.available = lambda: False
    assert not worker.run_once()
    assert path.exists() and path.with_suffix('.jpg').exists()
    with store.connect() as connection:
        row = connection.execute('SELECT * FROM observations').fetchone()
        assert row['status'] == 'pending' and row['attempts'] == 0
        assert row['error'] == 'key_unavailable'
        connection.execute('UPDATE observations SET retry_at=0')
    worker.observer.available = lambda: True
    assert worker.run_once()
    assert store.search('owner', ARGS)['counts'] == {'ready': 1}


def test_failures_are_bounded_and_never_stored_as_observations(evidence):
    store, worker, path, _ = evidence

    def fail(*_):
        raise RuntimeError('private transport detail')

    worker.observer.analyze = fail
    for _ in range(3):
        with store.connect() as connection:
            connection.execute('UPDATE observations SET retry_at=0')
        assert worker.run_once()
    assert not worker.run_once()
    result = store.search('owner', ARGS)
    assert result['counts'] == {'failed': 1} and result['observations'] == []
    with store.connect() as connection:
        row = connection.execute('SELECT * FROM observations').fetchone()
        assert row['analysis'] is None and row['error'] == 'analysis_failed'
    assert path.with_suffix('.jpg').exists()


def test_incomplete_changed_and_simulated_evidence_cannot_be_recalled(evidence):
    store, worker, path, _ = evidence
    capture(store.directory, simulated=True)
    bad, _ = capture(store.directory)
    bad.with_suffix('.jpg').write_bytes(b'changed')
    (store.directory / 'captures' / 'incomplete.json.tmp').write_text('{}')
    worker.run_once()
    assert store.search('owner', ARGS)['counts'] == {'failed': 1, 'ready': 1, 'simulated': 1}
    path.with_suffix('.jpg').unlink()
    assert store.search('owner', ARGS)['observations'] == []


def test_image_symlink_and_tampered_manifest_are_rejected(evidence, tmp_path):
    _, _, path, metadata = evidence
    original = path.with_suffix('.jpg').read_bytes()
    outside = tmp_path / 'outside.jpg'
    outside.write_bytes(original)
    path.with_suffix('.jpg').unlink()
    path.with_suffix('.jpg').symlink_to(outside)
    with pytest.raises(ValueError):
        read_capture(path)
    path.with_suffix('.jpg').unlink()
    path.with_suffix('.jpg').write_bytes(original)
    metadata['observation_id'] = uuid4().hex
    path.write_text(json.dumps(metadata))
    with pytest.raises(ValueError):
        read_capture(path)


def test_explicit_simulation_database_is_durable_and_cannot_be_used_as_real(tmp_path):
    store = PatrolMemoryStore(tmp_path, 'sim-owner', simulation=True)
    capture(tmp_path, simulated=True)
    capture(tmp_path, simulated=False)
    observer = SimpleNamespace(available=lambda: True, model='local-test-fixture',
                               analyze=lambda _metadata, _image: copy.deepcopy(ANALYSIS))
    worker = PatrolObservationWorker(store, observer, autostart=False)
    assert worker.run_once()
    assert not worker.run_once()
    reopened = PatrolMemoryStore(tmp_path, 'sim-owner', simulation=True)
    result = reopened.search('sim-owner', ARGS)
    assert result['environment'] == 'simulation'
    assert result['counts'] == {'ready': 1, 'real_excluded': 1}
    assert result['observations'][0]['simulated'] is True
    assert result['observations'][0]['analysis_model'] == 'local-test-fixture'
    with pytest.raises(ValueError, match='separate observation directory'):
        PatrolMemoryStore(tmp_path, 'sim-owner')


def test_real_database_cannot_be_repurposed_for_simulation(evidence):
    store, _, _, _ = evidence
    with pytest.raises(ValueError, match='separate observation directory'):
        PatrolMemoryStore(store.directory, 'owner', simulation=True)


def test_live_inspection_is_analyzed_before_patrol_sealing_and_credits_exact_photo(
        tmp_path, monkeypatch):
    """Join the real recorder, Agent worker and coverage reader using an offline model."""
    import numpy as np

    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / 'malbut_autonomy/malbut_patrol'))
    from malbut_patrol.coverage import CoverageGrid, CoveragePlanner, CoverageProfile
    from malbut_patrol.inspection import InspectionCalibration, InspectionEvidence
    from malbut_patrol.photo_recorder import PhotoRecorder

    calibration = InspectionCalibration(
        root / 'malbut_autonomy/malbut_patrol/config/inspection_sim_20261022.json', True)
    grid = CoverageGrid(np.zeros((30, 30)), .1, 0, 0)
    recorder = PhotoRecorder(tmp_path, inspection=True)
    store = PatrolMemoryStore(tmp_path, 'owner', simulation=True)
    calls = []

    def analyze(metadata, image):
        calls.append(metadata['observation_id'])
        assert image.startswith(b'\xff\xd8')
        assert 'analysis_batch' not in metadata and metadata['simulated']
        return copy.deepcopy(ANALYSIS)

    worker = PatrolObservationWorker(store, SimpleNamespace(
        available=lambda: True, model=calibration.model, analyze=analyze), autostart=False)
    try:
        recorder.begin(grid, 'map', '')
        camera = calibration.camera_record(640, 400, calibration.camera['hfov_rad'], .12)
        image = SimpleNamespace(width=640, height=400, step=1920, encoding='rgb8',
                                data=bytes([120] * 640 * 400 * 3),
                                header=SimpleNamespace(frame_id='optical',
                                                       stamp=SimpleNamespace(sec=1, nanosec=0)))
        assert recorder.offer(image, x=1.5, y=1.5, yaw=0, room_names=[], phase='OBSERVING',
                              simulated=True, inspection=camera)
        deadline = time.monotonic() + 5
        while recorder.status()['saved'] != 1 and time.monotonic() < deadline:
            time.sleep(.01)
        assert recorder.status()['saved'] == 1 and not recorder.status()['batch_ready']
        run, references = recorder.evidence()
        evidence = InspectionEvidence(tmp_path, calibration, run)
        assert not evidence.poll(references)
        assert worker.run_once()
        views = evidence.poll(references)
        assert len(views) == 1 and calls == [references[0]['observation_id']]
        planner = CoveragePlanner(grid, CoverageProfile(1, .95, .5), (1.5, 1.5),
                                  require_room_entry=False)
        planner.mark_observed(*views[0])
        assert planner.observed_area_m2 > 0
        assert not worker.run_once()
        assert not (tmp_path / 'runs' / (run['patrol_id'] + '.json')).exists()
    finally:
        worker.close()
        recorder.close()


def test_vision_uses_rotated_managed_key_and_sends_image_without_tools(tmp_path):
    environment = {'OPENAI_API_KEY': 'offline-first-key'}
    key = ManagedKey('openai', environ=environment, directory=tmp_path)
    calls = []

    def transport(url, headers, payload, timeout):
        assert url == 'https://api.openai.com/v1/responses' and timeout == 30
        assert not payload['store'] and not payload.get('tools')
        assert payload['input'][0]['content'][1]['type'] == 'input_image'
        assert payload['input'][0]['content'][1]['image_url'].startswith('data:image/jpeg;base64,')
        calls.append(headers['Authorization'])
        return {'status': 'completed', 'output': [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': json.dumps(ANALYSIS)}]}]}

    observer = OpenAIPatrolObserver(key, 'configured-vision-model', 'https://api.openai.com/v1',
                                    transport=transport)
    metadata = {'captured_at': datetime.now(timezone.utc).isoformat(),
                'capture_rooms': ['거실'], 'simulated': False}
    assert observer.analyze(metadata, b'image') == ANALYSIS
    environment['OPENAI_API_KEY'] = 'offline-rotated-key'
    observer.analyze(metadata, b'image')
    assert calls == ['Bearer offline-first-key', 'Bearer offline-rotated-key']
    with pytest.raises(ValueError):
        OpenAIPatrolObserver(key, 'model', 'https://untrusted.example/v1')


class QueryProvider:
    supports_memory = True

    def __init__(self, forbidden=False):
        self.calls = []
        self.forbidden = forbidden

    def complete(self, request, memories, conversation_turns, tools,
                 conversation_summary=None, *, memory_context=None):
        self.calls.append((request, tools, copy.deepcopy(memory_context)))
        if len(self.calls) % 2:
            decision = AgentDecision('tool_call', '조회', tool_name='search_patrol_observations',
                                     arguments=ARGS)
        elif self.forbidden:
            decision = AgentDecision('tool_call', '이동', tool_name='request_follow_person',
                                     arguments={})
        else:
            decision = AgentDecision('message', '오후 2시 순찰 사진에서 소파 위 리모컨을 봤어요.')
        return ProviderResult(decision, 'fixture', 'fixed', 1)


@pytest.mark.parametrize('forbidden', [False, True])
def test_real_dialogue_retrieves_after_restart_without_robot_action(evidence, forbidden):
    store, worker, _, metadata = evidence
    worker.run_once()
    runtime = build_orchestrator(Settings(database_path=':memory:'), http_server=False)
    runtime.conversation_store.create('owner', 'new-conversation')
    runtime.provider = QueryProvider(forbidden)
    configure_patrol_queries(runtime, PatrolMemoryStore(store.directory, 'owner'))
    request = AgentRequest(
        request_id='query-1', user_id='owner', conversation_id='new-conversation',
        turn_id='turn-1', utterance='리모컨 어디서 봤어?', robot_state=RobotState(),
        available_tools=('search_patrol_observations',))
    try:
        result = runtime.handle(request)
        assert result.decision.type == ('refusal' if forbidden else 'message')
        assert result.provider_result.memory_proposal is None
        followup = runtime.provider.calls[1]
        assert followup[0].available_tools == () and followup[1] == []
        assert followup[2]['patrol_observations_untrusted']['observations'][0][
            'observation_id'] == metadata['observation_id']
        assert result.safety.code == 'patrol_read_only'
        runtime.handle(request)
        assert len(runtime.provider.calls) == 2
    finally:
        runtime.close()


def test_payload_marks_observation_time_uncertainty_and_prompt_injection_boundary():
    request = AgentRequest(request_id='r', user_id='u', conversation_id='c', turn_id='t',
                           utterance='아까 뭐 봤어?', robot_state=RobotState(), available_tools=())
    payload = OpenAIResponsesProvider('offline-key', 'model').build_payload(
        request, [], [], [], memory_context={
            'mode': 'answer_only', 'patrol_observations_untrusted': {'observations': [ANALYSIS]}})
    assert not payload.get('tools')
    assert '촬영 시각' in payload['instructions']
    assert '명령 권한이 없습니다' in payload['instructions']
    assert '과거 관찰을 현재 상태로 단정하지' in payload['instructions']
    assert '리모컨' in payload['input']


def test_speech_worker_advertises_and_answers_visual_recall(evidence):
    from malbut_agent_server.speech_dialogue import DialogueWorker

    store, analysis_worker, _, _ = evidence
    analysis_worker.run_once()
    provider = QueryProvider()

    def factory():
        runtime = build_orchestrator(Settings(database_path=':memory:'), http_server=False)
        runtime.provider = provider
        configure_patrol_queries(runtime, store)
        return runtime

    dialogue = DialogueWorker(factory, 'owner')
    try:
        assert dialogue.submit('visual-recall-1', '리모컨 어디서 봤어?')
        deadline = time.monotonic() + 5
        replies = []
        while not replies and time.monotonic() < deadline:
            replies.extend(dialogue.drain())
            time.sleep(0.01)
        assert replies and '오후 2시' in replies[-1]['text']
        assert 'search_patrol_observations' in provider.calls[0][0].available_tools
        assert len(provider.calls) == 2
    finally:
        dialogue.close()


@pytest.mark.parametrize('simulation', [False, True])
def test_robot_configuration_reuses_managed_key_and_honors_disable(
        tmp_path, monkeypatch, simulation):
    from malbut_agent_server.factory import openai_key_for
    from malbut_agent_server.patrol_query import configure_patrol_from_settings
    import malbut_agent_server.patrol_observer as observer_module

    directory = tmp_path / 'observations'
    monkeypatch.setenv('MALBUT_PATROL_OBSERVATION_DIR', str(directory))
    monkeypatch.setenv('MALBUT_KEY_DIR', str(tmp_path / 'keys'))
    monkeypatch.setenv('MALBUT_PATROL_OBSERVATIONS', '1')
    monkeypatch.setenv('MALBUT_PATROL_SIMULATED', '1' if simulation else '0')
    monkeypatch.setenv('MALBUT_PATROL_VISION_MODEL', 'configured-camera-model')
    monkeypatch.setattr(observer_module, 'PatrolObservationWorker',
                        lambda store, observer: PatrolObservationWorker(
                            store, observer, autostart=False))
    runtime = build_orchestrator(Settings(database_path=':memory:'), http_server=False)
    settings = Settings(provider='openai', user_id='owner', openai_api_key='offline-team-key')
    try:
        configure_patrol_from_settings(runtime, settings, http_server=False)
        assert runtime.patrol_observation_worker.observer.key is openai_key_for(settings)
        assert runtime.patrol_observation_worker.observer.model == 'configured-camera-model'
        assert runtime.patrol_query_tools == ('search_patrol_observations',)
        assert runtime.patrol_memory.simulation is simulation
        assert not (tmp_path / 'keys').exists()  # Never create or copy a credential.
        monkeypatch.setenv('MALBUT_PATROL_OBSERVATIONS', '0')
        configure_patrol_from_settings(runtime, settings, http_server=False)
        assert not runtime.patrol_query_tools
    finally:
        runtime.close()
