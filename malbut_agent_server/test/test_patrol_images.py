"""Verify independent input boundaries and source association without paid calls."""

import copy
import hashlib
import io
import json
from pathlib import Path

import numpy as np

import pytest

from malbut_agent_server import patrol_images
from malbut_agent_server.patrol_memory import PatrolMemoryStore
from malbut_agent_server.patrol_observer import OpenAIPatrolObserver, PatrolObservationWorker
from test_patrol_batch import batch
from test_patrol_spatial import REGIONS


@pytest.mark.parametrize('count', [0, 25])
def test_frame_limits_reject_before_api_call(tmp_path, count):
    captures, _ = batch(tmp_path, max(1, count))
    calls = []
    observer = OpenAIPatrolObserver('offline-key', 'fixture', 'https://api.openai.com/v1',
                                    transport=lambda *args: calls.append(args))
    with pytest.raises(ValueError, match='frame count'):
        observer.analyze_batch(captures[:count])
    assert not calls


@pytest.mark.parametrize('fault', ['duplicate', 'map', 'simulation', 'hash', 'invalid_jpeg'])
def test_inconsistent_or_changed_images_are_rejected(tmp_path, fault):
    captures, _ = batch(tmp_path, 2)
    captures = copy.deepcopy(captures)
    if fault == 'duplicate':
        captures[1][0]['observation_id'] = captures[0][0]['observation_id']
    elif fault == 'map':
        captures[1][0]['map_id'] = 'b' * 64
    elif fault == 'simulation':
        captures[1][0]['simulated'] = False
    elif fault == 'hash':
        captures[1][0]['image_sha256'] = 'b' * 64
    else:
        image = b'\xff\xd8invalid\xff\xd9'
        captures[1] = (captures[1][0], image)
        captures[1][0]['image_sha256'] = hashlib.sha256(image).hexdigest()
    with pytest.raises(ValueError):
        patrol_images.make_image_inputs(captures)


def test_total_payload_bound_never_silently_resizes_or_splits(tmp_path, monkeypatch):
    captures, _ = batch(tmp_path, 2)
    monkeypatch.setattr(patrol_images, 'MAX_IMAGE_BYTES', sum(len(b) for _, b in captures) - 1)
    calls = []
    observer = OpenAIPatrolObserver('offline-key', 'fixture', 'https://api.openai.com/v1',
                                    transport=lambda *args: calls.append(args))
    with pytest.raises(ValueError, match='request too large'):
        observer.analyze_batch(captures)
    assert not calls


@pytest.mark.parametrize('count', [2, 24])
@pytest.mark.parametrize('kind', ['box', 'person'])
def test_each_image_uses_its_own_depth_then_pixels_are_released(
        tmp_path, monkeypatch, count, kind):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / 'malbut_autonomy/malbut_patrol'))
    from malbut_patrol.transient_media import TransientMedia

    captures, manifest = batch(tmp_path, count)
    regions = json.dumps({'version': 1, 'regions': REGIONS}, ensure_ascii=False).encode()
    media = TransientMedia(tmp_path)
    try:
        for index, (metadata, image) in enumerate(captures):
            stream = io.BytesIO()
            distance = [.5, 2., 1., float('nan')][index % 4]
            measured = np.full((400, 640), distance, np.float32)
            if kind == 'person':
                # A body has foreground support within its box. A full-frame
                # flat wall must never serve as a person's measured surface.
                measured[:] = 4.
                measured[145:255, 235:405] = distance
            np.save(stream, measured, allow_pickle=False)
            depth = stream.getvalue()
            metadata.update(version=2, media_storage='volatile_v1', media_expires_at=10**12,
                            capture_rooms=['거실'])
            metadata['capture_regions'] = [{'id': 'living', 'name': '거실'}]
            metadata['regions_sha256'] = hashlib.sha256(regions).hexdigest()
            metadata['image_stamp'] = {'sec': 42 + index, 'nanosec': 0}
            metadata['spatial'] = {
                'status': 'available', 'depth_shape': [400, 640],
                'depth_stamp': metadata['image_stamp'],
                'depth_sha256': hashlib.sha256(depth).hexdigest(),
                'intrinsics': {'fx': 400., 'fy': 400., 'cx': 320., 'cy': 200.},
                'map_from_camera': {'translation': [-1., 0., .2],
                                    'rotation_xyzw': [-.5, .5, -.5, .5]},
                'optical_frame': 'optical'}
            identity = metadata['observation_id']
            media.put(identity, {'.jpg': image, '.depth.npy': depth, '.regions.json': regions})
            path = tmp_path / 'captures' / (identity + '.json')
            path.with_suffix('.jpg').unlink()
            data = json.dumps(metadata, ensure_ascii=False).encode()
            path.write_bytes(data)
            manifest['captures'][index]['manifest_sha256'] = hashlib.sha256(data).hexdigest()
        (tmp_path / 'runs' / (manifest['patrol_id'] + '.json')).write_text(json.dumps(manifest))
        calls = []

        def transport(_url, _headers, payload, _timeout):
            calls.append(payload)
            frames = []
            for part in payload['input'][0]['content'][1::2]:
                frames.append({'frame_id': json.loads(part['text'])['frame_id'],
                               'summary': '상자가 보인다.', 'uncertainties': [], 'objects': [{
                                   'name': '대상', 'kind': kind, 'confidence': 'high',
                                   'location': '중앙', 'image_bbox': [350, 350, 650, 650],
                                   'image_anchors': [[420, 420], [580, 420], [500, 550]]}]})
            return {'status': 'completed', 'output': [{'type': 'message', 'content': [{
                'type': 'output_text', 'text': json.dumps({'frames': frames[::-1]})}]}]}

        observer = OpenAIPatrolObserver('offline-key', 'fixture', 'https://api.openai.com/v1',
                                        transport=transport)
        store = PatrolMemoryStore(tmp_path, 'owner', simulation=True)
        assert PatrolObservationWorker(store, observer, autostart=False).run_once()
        assert len(calls) == 1 and not media._items
        assert not list(tmp_path.rglob('*.jpg')) and not list(tmp_path.rglob('*.npy'))
        reopened = PatrolMemoryStore(tmp_path, 'owner', simulation=True)
        with reopened.connect() as db:
            for index, (metadata, _) in enumerate(captures):
                expected = ['living', 'workroom', None, None][index % 4]
                row = db.execute('SELECT * FROM observations WHERE id=?',
                                 (metadata['observation_id'],)).fetchone()
                assert row['status'] == 'ready'
                item = json.loads(row['analysis'])['objects'][0]
                if expected:
                    assert item['spatial']['status'] == 'located'
                    assert item['spatial']['region']['id'] == expected
                else:
                    assert item['spatial']['status'] == 'unknown'
                    assert item['spatial']['region'] is None
                assert json.loads(row['metadata'])['capture_rooms'] == ['거실']
        result = reopened.search('owner', {
            'query': '작업실 대상', 'since': None, 'until': None, 'limit': 1})
        assert len(result['observations']) == 1
        assert result['observations'][0]['objects'][0]['spatial']['region']['id'] == 'workroom'
        assert result['observations'][0]['photo'] is None
    finally:
        media.close()
