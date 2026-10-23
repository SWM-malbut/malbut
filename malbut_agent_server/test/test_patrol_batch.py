"""Verify one-call patrol analysis, atomic persistence and contact-sheet geometry."""

import hashlib
import json
import math
from uuid import uuid4

import cv2
import numpy as np
import pytest

from malbut_agent_server.patrol_batch import complete_batch
from malbut_agent_server.patrol_memory import PatrolMemoryStore
from malbut_agent_server.patrol_mosaic import make_mosaic, source_bbox, split_results
from malbut_agent_server.patrol_observer import OpenAIPatrolObserver, PatrolObservationWorker

from test_patrol_memory import capture as legacy_capture


def batch(directory, count=24, *, sealed=True, patrol_id=None):
    identity = patrol_id or uuid4().hex
    captures, references = [], []
    for index in range(count):
        path, metadata = legacy_capture(directory, simulated=True)
        frame = np.full((400, 640, 3), (0, 0, 120 + index), np.uint8)
        ok, encoded = cv2.imencode('.jpg', frame)
        assert ok
        image = encoded.tobytes()
        path.with_suffix('.jpg').write_bytes(image)
        metadata.update(patrol_id=identity, analysis_batch='patrol_mosaic_v1',
                        image_sha256=hashlib.sha256(image).hexdigest())
        data = json.dumps(metadata, ensure_ascii=False).encode()
        path.write_bytes(data)
        captures.append((metadata, image))
        references.append({'observation_id': path.stem,
                           'manifest_sha256': hashlib.sha256(data).hexdigest()})
    manifest = {'version': 1, 'patrol_id': identity, 'outcome': 'completed',
                'captures': references, 'capture_failures': 0}
    if sealed:
        (directory / 'runs').mkdir(exist_ok=True)
        (directory / 'runs' / (identity + '.json')).write_text(json.dumps(manifest))
    return captures, manifest


def analysis_for(layout):
    entries = []
    for frame in layout['frames']:
        x0, y0, x1, y1 = frame['photo_rect']
        pixels = [x0 + .35*(x1-x0), y0 + .35*(y1-y0),
                  x0 + .65*(x1-x0), y0 + .65*(y1-y0)]
        bbox = [round(v / extent * 1000) for v, extent in zip(
            pixels, [layout['width'], layout['height']] * 2)]
        entries.append({'frame_id': frame['frame_id'], 'summary': '빨간 상자가 보인다.',
                        'objects': [{'name': '빨간 상자', 'location': '사진 가운데',
                                     'confidence': 'high', 'sheet_bbox': bbox}],
                        'uncertainties': []})
    return {'frames': list(reversed(entries))}  # Reply order must not change source association.


def observer(calls, *, mutate=lambda response: response):
    def transport(_url, _headers, payload, timeout):
        calls.append(payload)
        assert timeout == 90 and not payload.get('tools') and not payload['store']
        contents = payload['input'][0]['content']
        assert sum(c['type'] == 'input_image' for c in contents) == 1
        context = json.loads(contents[0]['text'])
        layout = {'frames': context['frames'],
                  'width': context['sheet_size'][0], 'height': context['sheet_size'][1]}
        response = {'status': 'completed', 'usage': {
            'input_tokens': 2500, 'output_tokens': 1200, 'total_tokens': 3700,
            'input_tokens_details': {'cached_tokens': 0}},
            'output': [{'type': 'message', 'content': [
                {'type': 'output_text', 'text': json.dumps(analysis_for(layout))}]}]}
        return mutate(response)
    return OpenAIPatrolObserver('offline-key', 'fixture-vision', 'https://api.openai.com/v1',
                                transport=transport)


def test_24_photos_use_one_image_and_one_call_then_survive_restart(tmp_path):
    captures, manifest = batch(tmp_path)
    store = PatrolMemoryStore(tmp_path, 'owner', simulation=True)
    calls = []
    worker = PatrolObservationWorker(store, observer(calls), autostart=False)
    assert worker.run_once()
    assert len(calls) == 1 and not worker.run_once()
    with store.connect() as db:
        rows = db.execute('SELECT * FROM observations').fetchall()
        saved = db.execute('SELECT * FROM patrol_analysis_batches').fetchone()
    assert len(rows) == 24 and all(row['status'] == 'ready' for row in rows)
    assert json.loads(saved['usage'])['total_tokens'] == 3700
    layout = json.loads(saved['mosaic'])
    assert math.ceil(layout['width']/32) * math.ceil(layout['height']/32) <= 2500
    assert len(layout['frames']) == 24 and max(layout['width'], layout['height']) <= 2048
    photo = tmp_path / 'batches' / (manifest['patrol_id'] + '.jpg')
    assert hashlib.sha256(photo.read_bytes()).hexdigest() == layout['sha256']
    for row in rows:
        obj = json.loads(row['analysis'])['objects'][0]
        assert np.allclose(obj['image_bbox'], [350, 350, 650, 650], atol=7)
        assert obj['spatial']['status'] == 'unknown'  # No depth in the legacy fixture.
    reopened = PatrolMemoryStore(tmp_path, 'owner', simulation=True)
    assert not PatrolObservationWorker(reopened, observer(calls), autostart=False).run_once()
    result = reopened.search('owner', {'query': '상자', 'since': None, 'until': None, 'limit': 5})
    assert result['observations'] and result['counts'] == {'ready': 24}
    assert {i['observation_id'] for i in result['observations']} <= {
        meta['observation_id'] for meta, _ in captures}


def test_active_patrol_is_not_analyzed_and_separate_runs_never_share_a_call(tmp_path):
    _, first = batch(tmp_path, 2, sealed=False)
    batch(tmp_path, 3)
    store = PatrolMemoryStore(tmp_path, 'owner', simulation=True)
    calls = []
    worker = PatrolObservationWorker(store, observer(calls), autostart=False)
    assert worker.run_once() and len(calls) == 1
    assert not worker.run_once() and len(calls) == 1
    (tmp_path / 'runs' / (first['patrol_id'] + '.json')).write_text(json.dumps(first))
    assert worker.run_once() and len(calls) == 2
    assert [len(json.loads(call['input'][0]['content'][0]['text'])['frames'])
            for call in calls] == [3, 2]


def test_batch_retains_cache_writes_for_cost_accounting(tmp_path):
    batch(tmp_path, 2)

    def with_writes(response):
        response['usage']['input_tokens_details'] = {
            'cached_tokens': 400, 'cache_write_tokens': 1500,
            'cache_write_tokens_details': {'ttl_30m_tokens': 1000, 'ttl_12h_tokens': 500}}
        return response

    store = PatrolMemoryStore(tmp_path, 'owner', simulation=True)
    worker = PatrolObservationWorker(store, observer([], mutate=with_writes), autostart=False)
    assert worker.run_once()
    with store.connect() as db:
        usage = json.loads(db.execute('SELECT usage FROM patrol_analysis_batches').fetchone()[0])
    assert usage == dict(input_tokens=2500, output_tokens=1200, total_tokens=3700,
                         cached_input_tokens=400, cache_write_tokens=1500,
                         cache_write_30m_tokens=1000, cache_write_12h_tokens=500)


@pytest.mark.parametrize('fault', ['missing', 'duplicate', 'unknown', 'incomplete'])
def test_invalid_batch_response_never_partially_saves_observations(tmp_path, fault):
    batch(tmp_path, 3)

    def mutate(response):
        if fault == 'incomplete':
            response['status'] = 'incomplete'
        else:
            value = json.loads(response['output'][0]['content'][0]['text'])
            if fault == 'missing':
                value['frames'].pop()
            elif fault == 'duplicate':
                value['frames'][1] = value['frames'][0]
            else:
                value['frames'][0]['frame_id'] = 'F99'
            response['output'][0]['content'][0]['text'] = json.dumps(value)
        return response

    store = PatrolMemoryStore(tmp_path, 'owner', simulation=True)
    calls = []
    worker = PatrolObservationWorker(store, observer(calls, mutate=mutate), autostart=False)
    for attempt in range(3):
        assert worker.run_once()
        with store.connect() as db:
            count = db.execute(
                "SELECT count(*) FROM observations WHERE status='ready'").fetchone()[0]
            assert count == 0
            db.execute('UPDATE observations SET retry_at=0')
    assert not worker.run_once() and len(calls) == 3
    assert store.search('owner', {'query': None, 'since': None, 'until': None,
                                  'limit': 5})['counts'] == {'failed': 3}


def test_bbox_in_other_frame_or_label_cannot_borrow_another_photos_depth(tmp_path):
    captures, _ = batch(tmp_path, 4)
    _, layout = make_mosaic(captures)
    frame = layout['frames'][0]
    assert source_bbox([750, 750, 850, 850], frame, layout) is None
    assert source_bbox([0, 0, 100, 10], frame, layout) is None
    value = analysis_for(layout)
    value['frames'][-1]['objects'][0]['sheet_bbox'] = [750, 750, 850, 850]
    result = split_results(value, layout)
    assert result[frame['observation_id']]['objects'][0]['image_bbox'] is None


def test_batch_commit_is_atomic_and_sealed_files_cannot_change(tmp_path):
    captures, manifest = batch(tmp_path, 2)
    store = PatrolMemoryStore(tmp_path, 'owner', simulation=True)
    store.ingest()
    _, layout = make_mosaic(captures)
    results = split_results(analysis_for(layout), layout)
    with store.connect() as db:
        db.execute("UPDATE observations SET status='failed' WHERE id=?", (next(iter(results)),))
    with pytest.raises(ValueError, match='state changed'):
        complete_batch(store, manifest, results, 'fixture', {}, layout)
    with store.connect() as db:
        count = db.execute("SELECT count(*) FROM observations WHERE status='ready'").fetchone()[0]
        assert count == 0
        assert db.execute('SELECT count(*) FROM patrol_analysis_batches').fetchone()[0] == 0
        db.execute("UPDATE observations SET status='pending'")
    path = tmp_path / 'captures' / (manifest['captures'][0]['observation_id'] + '.json')
    path.write_text(path.read_text() + '\n')
    calls = []
    assert PatrolObservationWorker(store, observer(calls), autostart=False).run_once()
    assert not calls


def test_restored_bbox_still_locates_object_in_different_region(tmp_path):
    from test_patrol_spatial import capture as spatial_fixture
    from malbut_agent_server.patrol_spatial import localize_objects, read_spatial

    path, metadata = spatial_fixture.__wrapped__(tmp_path)
    _, encoded = cv2.imencode('.jpg', np.full((100, 100, 3), (0, 0, 200), np.uint8))
    _, layout = make_mosaic([(metadata, encoded.tobytes())])
    results = split_results(analysis_for(layout), layout)
    regions, depth = read_spatial(path, metadata)
    analysis = localize_objects(results[path.stem], metadata, regions, depth)
    assert metadata['capture_rooms'] == ['거실']
    assert analysis['objects'][0]['spatial']['region']['name'] == '작업실'


@pytest.mark.parametrize('count', [1, 4, 9, 10, 24])
def test_separated_cards_keep_image_budget_and_geometry(tmp_path, count):
    captures, _ = batch(tmp_path, count)
    image, layout = make_mosaic(captures)
    decoded = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR)
    assert layout['version'] == 2
    assert math.ceil(layout['width']/32) * math.ceil(layout['height']/32) <= 2500
    assert max(layout['width'], layout['height']) <= 2048
    for i, frame in enumerate(layout['frames']):
        left, top, right, bottom = frame['photo_rect']
        # Aspect ratio and the entire image survive; labels never replace pixels.
        assert (right-left) / (bottom-top) == pytest.approx(640/400, abs=.01)
        pixels = decoded[top+16:bottom-16, left+16:right-16]
        assert np.max(np.abs(pixels.astype(int) - (0, 0, 120+i))) <= 4
        # Coordinates supplied to the model use its response's normalized units.
        assert np.allclose(source_bbox(frame['photo_rect_1000'], frame, layout),
                           [0, 0, 1000, 1000], atol=6)
        if i % layout['columns']:
            previous = layout['frames'][i-1]
            gap_left, gap_right = previous['photo_rect'][2], left
            assert gap_right - gap_left >= 24
            midpoint = (gap_left+gap_right)//2
            assert decoded[(top+bottom)//2, midpoint].max() <= 16
    if count == 24:
        assert layout['width'] == 1392 and layout['height'] == 1744
        assert all(f['photo_rect'][2]-f['photo_rect'][0] == 320 for f in layout['frames'])


def test_batch_request_declares_frame_before_its_contents(tmp_path):
    batch(tmp_path, 2)
    store = PatrolMemoryStore(tmp_path, 'owner', simulation=True)
    calls = []
    assert PatrolObservationWorker(store, observer(calls), autostart=False).run_once()
    schema = calls[0]['text']['format']['schema']['properties']['frames']['items']
    assert next(iter(schema['properties'])) == 'frame_id'
    context = json.loads(calls[0]['input'][0]['content'][0]['text'])
    assert all('photo_rect_1000' in f and 'grid_row' in f and 'grid_column' in f
               for f in context['frames'])


@pytest.mark.parametrize('wrong_frame', [False, True])
def test_person_anchors_are_restored_only_from_their_own_card(tmp_path, wrong_frame):
    captures, _ = batch(tmp_path, 4)
    _, layout = make_mosaic(captures)
    frame, other = layout['frames'][:2]
    value = analysis_for(layout)
    obj = value['frames'][-1]['objects'][0]
    obj['kind'] = 'person'
    x0, y0, x1, y1 = (other if wrong_frame else frame)['photo_rect']
    obj['sheet_anchors'] = [[round((x0+u*(x1-x0))/layout['width']*1000),
                             round((y0+v*(y1-y0))/layout['height']*1000)]
                            for u, v in [(.4, .4), (.6, .6)]]
    restored = split_results(value, layout)[frame['observation_id']]['objects'][0]
    if wrong_frame:
        assert restored['image_bbox'] is None and restored['image_anchors'] == []
    else:
        assert np.allclose(restored['image_anchors'], [[400, 400], [600, 600]], atol=7)
