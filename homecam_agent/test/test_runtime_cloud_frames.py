"""Offline tests for runtime-prompt Cloud evaluation; no inference calls."""
import base64
import io
import json
from pathlib import Path
import sys

import cv2
import numpy as np
from PIL import Image
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import evaluate_runtime_cloud_frames as runner


def envelope(assessment, explanation='영상에서 확인한 행동', fenced=False):
    text = json.dumps(dict(assessment=assessment, explanation=explanation), ensure_ascii=False)
    if fenced:
        text = '```json\n' + text + '\n```'
    return json.dumps(dict(done=True, done_reason='stop', message=dict(
        role='assistant', content=text))).encode()


@pytest.mark.parametrize('label', ['observed_fall', 'suspected_fall', 'normal_activity'])
@pytest.mark.parametrize('fenced', [False, True])
def test_three_classes_not_collapsed(label, fenced):
    result = runner.assess(envelope(label, fenced=fenced))
    assert result['valid'] and result['prediction']['label'] == label


def test_unobservable_not_normal():
    result = runner.assess(envelope('unobservable'))
    assert result['valid'] and result['prediction']['label'] is None
    assert result['prediction']['outcome'] == 'unobservable'


@pytest.mark.parametrize('body', [b'broken', envelope('invented'), envelope('normal_activity', '')])
def test_invalid_is_not_repaired(body):
    result = runner.assess(body)
    assert not result['valid'] and result['prediction'] is None


def test_letterbox_keeps_entire_image():
    image = np.full((720, 1280, 3), 255, dtype=np.uint8)
    result = runner.letterbox(image)
    assert result.shape == (400, 640, 3)
    assert np.all(result[20:380] == 255)
    assert np.all(result[:20] == 0) and np.all(result[380:] == 0)
    assert np.array_equal(runner.letterbox(result), result)


@pytest.mark.parametrize('count', [6, 12])
def test_short_clip_actual_frames_and_no_labels(tmp_path, count):
    path = tmp_path / 'neutral.avi'
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'MJPG'), 12, (640, 400))
    assert writer.isOpened()
    for index in range(61):
        writer.write(np.full((400, 640, 3), index * 3, dtype=np.uint8))
    writer.release()
    meta = dict(source_path=path.name, sha256=runner.sha(path), frames=61,
                fps=12., width=640, height=400, case_id='SECRET_CASE_ID',
                label='SECRET_GT', reason='SECRET_REASON')
    payload, samples = runner.extract_request(tmp_path, meta, count)
    assert samples['history_incomplete'] and samples['available_span_s'] == 5
    indices = [r['frame_index'] for r in samples['frames']]
    assert len(set(indices)) == count and indices == sorted(indices)
    assert indices[0] == 0 and indices[-1] == 60
    assert payload['model'] == 'gemma4:31b-cloud'
    assert payload['think'] is False and 'format' not in payload
    assert 'SECRET' not in json.dumps(payload)
    message = payload['messages'][1]
    metadata = json.loads(message['content'][len(runner.adapter.USER_PREFIX):])
    assert metadata['sensors'] is None and metadata['audio_included'] is False
    assert metadata['purpose'] == 'crosscheck'
    assert [r['offset_s'] for r in metadata['frames']][::count-1] == [5, 10]
    assert len(message['images']) == count
    for encoded in message['images']:
        with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
            assert image.size == (640, 400)


@pytest.mark.parametrize('count', [True, 0, 7, 6.0])
def test_reject_unplanned_frame_counts(tmp_path, count):
    with pytest.raises(ValueError, match='frames must be 6 or 12'):
        runner.extract_request(tmp_path, {}, count)


def test_timeout_remains_in_denominator():
    labels = {'A': dict(label='observed_fall'), 'B': dict(label='suspected_fall')}
    rows = [dict(case_id='A', status='timeout', valid=False, prediction=None, request_s=20),
            dict(case_id='B', status='responded', request_s=3,
                 **runner.assess(envelope('suspected_fall')))]
    summary = runner.scoring.score(rows, labels, 'full')
    assert summary['classification']['numerator'] == 1
    assert summary['classification']['denominator'] == 2
    assert summary['checking']['unresolved_by_reason'] == {'timeout': 1}
