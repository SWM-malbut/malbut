"""Suite mechanics only: no downloads, VLM calls, labels changes or performance claims."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import run_ollama_fall_suite as suite  # noqa: E402


@pytest.mark.parametrize('last', range(63))
def test_prefix_contains_no_future_or_duplicate_images(last):
    selected = suite.prefix_indices(last)
    assert selected[0] == 0 and selected[-1] == last
    assert selected == sorted(set(selected))
    assert len(selected) == min(12, last + 1)


@pytest.mark.parametrize('invalid', [-1, 0.5, True])
def test_invalid_source_frame_rejected(invalid):
    with pytest.raises(ValueError):
        suite.prefix_indices(invalid)


def test_parity_ignores_only_random_id_prefixes_not_numerical_evidence():
    a = dict(targetTrackId='a' * 12 + '-1', observationId='b' * 32 + '-frame-25',
             candidateId='c' * 32 + '-candidate-3', evidence=dict(confidence=.1))
    b = dict(targetTrackId='d' * 12 + '-1', observationId='e' * 32 + '-frame-25',
             candidateId='f' * 32 + '-candidate-3', evidence=dict(confidence=.1))
    assert suite.normalize_ids(a) == suite.normalize_ids(b)
    b['evidence']['confidence'] = .2
    assert suite.normalize_ids(a) != suite.normalize_ids(b)
    assert suite.normalize_ids('a' * 64) == 'a' * 64


def test_no_trigger_is_not_vlm_normal_and_missed_fall_remains_in_denominator():
    labels = dict(SYN001=dict(label='observed_fall'), SYN002=dict(label='normal_activity'),
                  SYN003=dict(label='suspected_fall'))
    rows = [dict(case_id=cid, status='not_triggered', valid=False) for cid in labels]
    result = suite.metrics(rows, labels)
    assert result['eligible'] == 2 and result['invoked_eligible'] == 0
    assert result['invocations'] == 0 and result['valid_correct'] == 0
    assert result['pipeline_correct'] == 1
    assert all(r['assessment'] is None for r in result['rows'])


def test_invalid_correct_label_is_counted_as_error_not_removed():
    labels = dict(SYN001=dict(label='observed_fall'), SYN002=dict(label='found_down'))
    rows = [dict(case_id=cid, status='responded', valid=False, request_s=1,
                 prediction=dict(fall=dict(assessment=answer)))
            for cid, answer in [('SYN001', 'confirmed_fall'), ('SYN002', 'found_down')]]
    result = suite.metrics(rows, labels)
    assert result['raw_correct'] == 2
    assert result['eligible'] == 2 and result['invoked_eligible'] == 2
    assert result['valid_correct'] == result['pipeline_correct'] == 0


def test_call_sends_prefix_only_and_saves_raw_before_validation(tmp_path, monkeypatch):
    args = SimpleNamespace(dataset=tmp_path, endpoint='http://127.0.0.1:21434')
    contract = dict(model=dict(name='fake:2b'), options={}, thinking='disabled', mode='gated')
    meta = dict(case_id='SYN001', fps=12, frames=63, sha256='a' * 64)
    frame = dict(frame_index=24, timestamp_s=2, jpeg_sha256='b' * 64)
    seen = []

    def frames(dataset, value, last):
        assert value is meta and last == 24
        return ['actual-image'], [frame]

    def api(endpoint, path, payload, **kw):
        assert path == '/api/chat'
        seen.append(payload)
        return dict(done=True, done_reason='stop', message=dict(content='{}'))

    def validate(raw, duration):
        assert duration == 2.083
        assert (tmp_path / 'SYN001.response.json').exists()
        raise RuntimeError('simulated validator bug')

    monkeypatch.setattr(suite, 'extract_prefix', frames)
    monkeypatch.setattr(suite, 'api', api)
    monkeypatch.setattr(suite, 'assess_response', validate)
    with pytest.raises(RuntimeError, match='validator bug'):
        suite.call_case(args, tmp_path, contract, meta, 24,
                        candidate=dict(secret_label_not_for_prompt='observed_fall'), pose_ms=7)
    assert seen[0]['think'] is False
    assert 'secret_label_not_for_prompt' not in json.dumps(seen[0])
    assert 'SYN001' not in json.dumps(seen[0])
    source = json.loads((tmp_path / 'SYN001.input.json').read_text())
    assert source['available_through_frame'] == 24
    assert source['candidate_sent_to_model'] is False
    assert source['frames'] == [frame]
    with pytest.raises(ValueError, match='silent repeat'):
        suite.call_case(args, tmp_path, contract, meta, 24)
    assert len(seen) == 1


def test_evidence_hash_rejects_modified_results(tmp_path):
    suite.save(tmp_path / 'case.json', dict(valid=True))
    suite.save(tmp_path / 'completed.json', dict(files={
        'case.json': suite.sha(tmp_path / 'case.json')}))
    suite.verify_completed(tmp_path)
    (tmp_path / 'case.json').write_text('{}')
    with pytest.raises(ValueError, match='evidence changed'):
        suite.verify_completed(tmp_path)


def test_report_metrics_do_not_modify_raw_records():
    row = dict(case_id='SYN001', status='responded', valid=True, request_s=2,
               prediction=dict(fall=dict(assessment='found_down')))
    before = copy.deepcopy(row)
    result = suite.metrics([row], {'SYN001': dict(label='found_down')})
    assert row == before and result['valid_correct'] == 1
