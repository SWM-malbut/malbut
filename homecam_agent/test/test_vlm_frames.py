"""Local frame-comparison contract tests; no VLM calls or claimed model accuracy."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import replay_vlm_frames as runner  # noqa: E402


def valid_value():
    return dict(subjects=dict(person=1, pet=0, other=0), events=[],
                fall=dict(assessment='normal_activity', confidence=.1, recovery='unknown'),
                posture_end='standing', risk='none', risk_confidence=.9,
                camera_motion_observed='none', explanation_ko='사람이 서 있다.',
                evidence_ko=['서 있는 자세'], uncertainty_flags=[])


def response(value=None, **kwargs):
    return dict(message=dict(content=json.dumps(value or valid_value())),
                done=True, done_reason='stop', **kwargs)


def test_uniform_indices_include_both_ends_without_duplicates():
    assert runner.frame_indices(63) == [0, 6, 11, 17, 23, 28, 34, 39, 45, 51, 56, 62]
    for count in (2, 12, 32, 62, 63):
        frames = runner.frame_indices(count)
        assert frames[0] == 0 and frames[-1] == count - 1
        assert frames == sorted(set(frames))


@pytest.mark.parametrize('total,count', [(1, 12), (63, 1), (63, 33), (2.5, 12)])
def test_invalid_sampling_rejected(total, count):
    with pytest.raises(ValueError):
        runner.frame_indices(total, count)


@pytest.mark.parametrize('url', [
    'https://example.com', 'http://100.67.49.96:11434', 'http://127.0.0.1:80/api',
    'http://user:secret@127.0.0.1:80', 'http://127.0.0.1:80?token=x',
    'http://localhost:11434', 'http://127.0.0.1',
])
def test_non_loopback_or_ambiguous_endpoint_rejected(url):
    with pytest.raises(ValueError):
        runner.endpoint_url(url)


def test_loopback_tunnel_allowed():
    assert runner.endpoint_url('http://127.0.0.1:21434/') == 'http://127.0.0.1:21434'


def test_request_contains_only_rgb_and_timestamps_no_gt_or_yolo():
    frames = [dict(timestamp_s=0), dict(timestamp_s=5)]
    payload = runner.request_payload('local:model', ['abc', 'def'], frames, 5.25, {})
    assert payload['messages'][1]['images'] == ['abc', 'def']
    text = payload['messages'][1]['content']
    assert '0.000, 5.000' in text
    assert 'SYN' not in text and 'fall_s' not in text
    assert not any(key in payload for key in ('yolo_context', 'depth', 'labels'))
    assert payload['stream'] is False


def test_valid_response_and_semantic_disagreement_are_distinct():
    assert runner.assess_response(response(), 5.25)['valid']
    value = valid_value()
    value['fall']['assessment'] = 'confirmed_fall'
    result = runner.assess_response(response(value), 5.25)
    assert not result['valid'] and not result['schema_errors']
    assert 'fall:event_disagreement' in result['semantic_errors']


@pytest.mark.parametrize('bad', [
    {}, {'message': []}, {'message': {'content': 'garbage'}},
    {'message': {'content': '[]'}}, {'done': True, 'message': {'content': 'null'}},
])
def test_malformed_output_is_retained_as_invalid_not_normal(bad):
    result = runner.assess_response(bad, 5.25)
    assert not result['valid'] and result['prediction'] is None


def test_truncated_but_parseable_response_is_invalid():
    value = response()
    value['done_reason'] = 'length'
    assert not runner.assess_response(value, 5.25)['valid']


def test_output_is_private_and_cannot_be_overwritten(tmp_path):
    path = tmp_path / 'result.json'
    runner.save(path, {'x': 1})
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        runner.save(path, {'x': 2})
    assert json.loads(path.read_text()) == {'x': 1}


def test_prompt_hash_changes_when_words_or_schema_change():
    value = dict(system=runner.SYSTEM_PROMPT, user=runner.USER_TEMPLATE,
                 schema=runner.PREDICTION_JSON_SCHEMA)
    changed = copy.deepcopy(value)
    changed['user'] += ' changed'
    assert runner.digest(value) != runner.digest(changed)


def test_cloud_model_rejected_before_any_call(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail('network must not be called')
    monkeypatch.setattr(runner, 'api', fail)
    with pytest.raises(ValueError, match='cloud'):
        runner.local_model('http://127.0.0.1:21434', 'qwen:cloud')


def fake_run(tmp_path, monkeypatch):
    frozen = tmp_path / 'frozen'
    frozen.mkdir()
    runner.save(frozen / 'media.json', {'cases': [dict(
        case_id=case_id, source_path='hidden.mp4', sha256='a' * 64, frames=63, fps=12)
        for case_id in ('SYN001', 'SYN002')]})
    runner.save(frozen / 'evaluation_labels.json', {'do_not_read_into_inference': True})
    runner.save(frozen / 'freeze.json', {'files': {
        name: runner.sha(frozen / name) for name in ('media.json', 'evaluation_labels.json')}})
    monkeypatch.setattr(runner, 'local_model', lambda *args: {'digest': 'd' * 64})
    monkeypatch.setattr(runner, 'extract_frames', lambda *args: (
        ['image'], [{'timestamp_s': 0, 'frame_index': 0, 'jpeg_sha256': 'e' * 64}]))
    calls = []

    def api(endpoint, path, payload=None, **kwargs):
        if path == '/api/version':
            return {'version': 'fake'}
        assert path == '/api/chat'
        calls.append(payload)
        return response()

    monkeypatch.setattr(runner, 'api', api)
    args = SimpleNamespace(
        frozen=frozen, output=tmp_path / 'result', dataset=tmp_path, endpoint='unused',
        model='fake:local', host_description='test', frames=12, timeout=180, limit=1,
        resume=False, schema_in_prompt=False)
    return args, calls


def test_resume_skips_completed_calls_and_binds_contract(tmp_path, monkeypatch):
    args, calls = fake_run(tmp_path, monkeypatch)
    runner.run(args)
    assert len(calls) == 1
    args.resume = True
    runner.run(args)
    assert len(calls) == 2
    runner.run(args)
    assert len(calls) == 2
    assert (args.output / 'completed.json').exists()
    args.frames = 8
    with pytest.raises(ValueError, match='contract changed'):
        runner.run(args)


def test_interruption_marker_prevents_silent_duplicate_call(tmp_path, monkeypatch):
    args, calls = fake_run(tmp_path, monkeypatch)
    runner.run(args)
    runner.save(args.output / 'SYN002.input.json', {'interrupted': True})
    args.resume = True
    with pytest.raises(ValueError, match='interrupted attempt'):
        runner.run(args)
    assert len(calls) == 1


def test_request_failure_is_saved_before_batch_stops(tmp_path, monkeypatch):
    args, calls = fake_run(tmp_path, monkeypatch)
    api = runner.api

    def timeout(endpoint, path, payload=None, **kwargs):
        if path == '/api/chat':
            raise TimeoutError('do not include request body in error output')
        return api(endpoint, path, payload, **kwargs)

    monkeypatch.setattr(runner, 'api', timeout)
    with pytest.raises(RuntimeError, match='saved and stopped'):
        runner.run(args)
    record = json.loads((args.output / 'SYN001.result.json').read_text())
    assert not record['valid'] and record['status'] == 'request_failed'
    assert record['error_type'] == 'TimeoutError'
    assert not (args.output / 'SYN002.input.json').exists()


def test_raw_response_survives_validator_exception(tmp_path, monkeypatch):
    args, calls = fake_run(tmp_path, monkeypatch)

    def bug(*args):
        raise RuntimeError('validator bug')

    monkeypatch.setattr(runner, 'assess_response', bug)
    with pytest.raises(RuntimeError, match='validator bug'):
        runner.run(args)
    assert (args.output / 'SYN001.response.json').exists()
    assert not (args.output / 'SYN001.result.json').exists()


def test_scoring_separates_ambiguous_invalid_and_unobservable():
    from score_vlm_frames import summarize

    def row(label, assessment, valid):
        return dict(label=label, assessment=assessment, valid=valid, request_s=15,
                    schema_errors=[], semantic_errors=[])

    summary = summarize([
        row('observed_fall', 'confirmed_fall', True),
        row('observed_fall', 'found_down', False),
        row('observed_fall', 'unobservable', True),
        row('suspected_fall', 'normal_activity', True),
        row('normal_activity', 'found_down', True),
    ])
    fall = summary['groups']['observed_fall']
    assert fall['total'] == 3 and fall['valid'] == 2
    assert fall['accepted_fall_or_found_down'] == 1
    assert fall['raw_distribution_not_validated']['found_down'] == 1
    assert 'found_down' not in fall['accepted_distribution']
    assert summary['groups']['suspected_fall']['total'] == 1
    assert summary['groups']['normal_activity']['accepted_fall_or_found_down'] == 1


def test_frame_extraction_keeps_original_geometry_and_checks_source(tmp_path):
    import cv2
    import numpy as np

    path = tmp_path / 'video.avi'
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'MJPG'), 12, (64, 40))
    assert writer.isOpened()
    for index in range(24):
        writer.write(np.full((40, 64, 3), index * 8, dtype=np.uint8))
    writer.release()
    meta = dict(source_path='video.avi', sha256=runner.sha(path), frames=24,
                fps=12, width=64, height=40)
    images, frames = runner.extract_frames(tmp_path, meta, 12)
    assert len(images) == len(frames) == 12
    assert frames[0]['timestamp_s'] == 0 and frames[-1]['timestamp_s'] == 23 / 12
    assert len({f['jpeg_sha256'] for f in frames}) == 12
    for data in images:
        jpeg = np.frombuffer(runner.base64.b64decode(data), dtype=np.uint8)
        assert cv2.imdecode(jpeg, cv2.IMREAD_COLOR).shape == (40, 64, 3)
    meta['sha256'] = '0' * 64
    with pytest.raises(ValueError, match='source media changed'):
        runner.extract_frames(tmp_path, meta, 12)


def test_media_cannot_escape_dataset_through_symlink(tmp_path):
    outside = tmp_path / 'outside.avi'
    outside.touch()
    dataset = tmp_path / 'dataset'
    dataset.mkdir()
    (dataset / 'link.avi').symlink_to(outside)
    with pytest.raises(ValueError, match='escapes dataset'):
        runner.extract_frames(dataset, {'source_path': 'link.avi'}, 12)


def test_missing_or_non_vision_local_model_rejected(monkeypatch):
    monkeypatch.setattr(runner, 'api', lambda *args: {'models': []})
    with pytest.raises(ValueError, match='already be installed'):
        runner.local_model('http://127.0.0.1:21434', 'unknown')
    model = dict(name='fake', size=100, digest='a' * 64, details={'format': 'gguf'})
    monkeypatch.setattr(runner, 'api', lambda endpoint, path, *args: (
        {'models': [model]} if path == '/api/tags' else {'capabilities': ['completion']}))
    with pytest.raises(ValueError, match='vision'):
        runner.local_model('http://127.0.0.1:21434', 'fake')


def test_schema_grounding_changes_only_text_not_images_or_decoder():
    arguments = ('fake', ['img'], [{'timestamp_s': 0}], 1, {'temperature': 0})
    plain = runner.request_payload(*arguments)
    grounded = runner.request_payload(*arguments, schema_in_prompt=True)
    assert grounded['format'] == plain['format']
    assert grounded['messages'][1]['images'] == plain['messages'][1]['images']
    assert grounded['messages'][0] == plain['messages'][0]
    before, schema = grounded['messages'][1]['content'].split(runner.SCHEMA_PROMPT_PREFIX)
    assert before == plain['messages'][1]['content']
    assert json.loads(schema) == runner.PREDICTION_JSON_SCHEMA
    assert runner.digest(grounded) != runner.digest(plain)


def test_supplied_timestamp_precision_does_not_penalize_exact_reported_end():
    value = valid_value()
    value['events'] = [dict(
        type='other', subject='person', confidence=.9, start_s=0, end_s=5.167)]
    raw = response(value)
    assert 'event_time:outside_clip' in runner.assess_response(raw, 62 / 12)['semantic_errors']
    assert runner.assess_response(raw, round(62 / 12, 3))['valid']
    value['events'][0]['end_s'] = 5.168
    assert not runner.assess_response(response(value), round(62 / 12, 3))['valid']
