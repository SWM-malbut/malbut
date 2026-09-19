"""Replay policy/timestamps only; no real model calls."""
import copy
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import run_fall84_realtime as run  # noqa: E402
import stream_pose_candidates as stream  # noqa: E402


def test_only_first_trigger_frame_calls_no_labels_or_ranking():
    assert not run.first_call([], False)
    assert run.first_call([{'targetTrackId': 'weak'}], False)
    assert run.first_call([{'targetTrackId': 'a'}, {'targetTrackId': 'b'}], False)
    assert not run.first_call([{'targetTrackId': 'different'}], True)


def test_wire_schema_keeps_full_prompt_and_strict_client_length_validation():
    from fall_evaluation_v2 import PREDICTION_JSON_SCHEMA, validate_prediction
    expected = copy.deepcopy(PREDICTION_JSON_SCHEMA)
    del expected['properties']['explanation_ko']['maxLength']
    assert run.wire_schema() == expected
    assert PREDICTION_JSON_SCHEMA['properties']['explanation_ko']['maxLength'] == 4000
    assert validate_prediction(dict(outcome='classified', label='normal_activity', explanation_ko='a'*4001))
    assert not validate_prediction(dict(outcome='classified', label='normal_activity', explanation_ko='a'*4000))


def test_same_clock_ledger_preserves_interval_basis_not_request_duration_sum():
    replay = dict(replay_start_s=100., candidate_ready_s=102.5, input_last_pts_s=2.)
    result = dict(status='responded', valid=True, prediction=dict(label='suspected_fall'),
                  request_started_monotonic_s=103., response_received_monotonic_s=115.,
                  validation_finished_monotonic_s=115.2)
    value = run.ledger(replay, result, 'gated')
    assert value['valid_result_after_replay_start_s'] == pytest.approx(15.2)
    assert not value['realtime_robot_claim']
    assert 'not yet audited' in value['scope']
    result['valid'] = False
    assert run.ledger(replay, result, 'gated')['valid_result_after_replay_start_s'] is None
    result['request_started_monotonic_s'] = 3.
    with pytest.raises(ValueError, match='mixed replay clock'):
        run.ledger(replay, result, 'gated')


def test_timeout_is_not_a_valid_result_time():
    value = run.ledger(dict(replay_start_s=100.), dict(status='request_failed', valid=False,
                       request_started_monotonic_s=103., request_finished_monotonic_s=403.), 'gated')
    assert value['valid_result_after_replay_start_s'] is None
    assert value['outcome_label'] is None


def test_replay_pacing_never_releases_before_frame_due(monkeypatch):
    now, sleeps = [100.], []
    monkeypatch.setattr(stream.time, 'monotonic', lambda: now[0])
    def sleep(seconds):
        assert 0 < seconds <= .2
        sleeps.append(seconds)
        now[0] += seconds
    monkeypatch.setattr(stream.time, 'sleep', sleep)
    stream.wait_until(100.55)
    assert now[0] >= 100.55 and len(sleeps) == 3
    stream.wait_until(100.1)
    assert len(sleeps) == 3


def test_fresh_observations_must_match_reference():
    row = dict(case_id='SYN001', frame_index=0, observations=[dict(confidence=.1)],
               fall_analysis=dict(candidates=[]))
    run.compare_row(row, copy.deepcopy(row))
    other = copy.deepcopy(row)
    other['observations'][0]['confidence'] = .2
    with pytest.raises(ValueError, match='values differ'):
        run.compare_row(other, row)
    other = copy.deepcopy(row)
    other['frame_index'] = 2
    with pytest.raises(ValueError, match='order differs'):
        run.compare_row(other, row)


def test_in_process_dataclass_tuples_match_serialized_arrays_without_rounding():
    actual = dict(case_id='SYN001', frame_index=0,
                  observations=[dict(features=dict(anchor_names=('hip',), uncertainties=()), confidence=.123)],
                  fall_analysis=dict(candidates=[]))
    expected = json.loads(json.dumps(actual))
    run.compare_row(actual, expected)
    assert isinstance(actual['observations'][0]['features']['anchor_names'], tuple)
    expected['observations'][0]['confidence'] += 1e-10
    with pytest.raises(ValueError, match='values differ'):
        run.compare_row(actual, expected)


@pytest.mark.parametrize('mode', ['full', 'gated'])
def test_mode_records_every_case_and_gated_calls_are_fresh(tmp_path, monkeypatch, mode):
    calls, summaries = [], []
    model = dict(name='fake')
    args = SimpleNamespace(output=tmp_path/'run', model='fake', endpoint='not-used',
                           dataset=tmp_path, frozen=tmp_path, reference=tmp_path, pose_model=tmp_path/'fake.onnx')
    media = [dict(case_id=f'SYN{i:03d}', frames=2, fps=5, sha256='a'*64) for i in range(1, 85)]
    reference = [dict(case_id=m['case_id'], frame_index=0, timestamp_s=0., observations=[],
                      fall_analysis=dict(candidates=[{'targetTrackId': 't'}] if i%2 else []),
                      pipeline_ms=1.) for i, m in enumerate(media)]
    def generated(_):
        for row in copy.deepcopy(reference):
            row['replay_timing'] = dict(replay_start_s=time.monotonic(), candidate_ready_s=time.monotonic())
            yield row
    def invoke(_args, out, contract, meta, frame, **kw):
        calls.append((meta['case_id'], frame))
        run.suite.save(out/f'{meta["case_id"]}.result.json', dict(
            case_id=meta['case_id'], status='responded', valid=True,
            prediction=dict(outcome='classified', label='suspected_fall', explanation_ko='test'),
            request_started_monotonic_s=time.monotonic(),
            response_received_monotonic_s=time.monotonic(),
            validation_finished_monotonic_s=time.monotonic()))
    monkeypatch.setattr(run.suite, 'call_case', invoke)
    monkeypatch.setattr(run.suite, 'mode_contract', lambda *a: dict(source_sha256={}))
    monkeypatch.setattr(run.suite, 'local_model', lambda *a: model)
    monkeypatch.setattr(run, 'iter_rows', generated)
    monkeypatch.setattr(run, 'wait_until', lambda *a: None)
    monkeypatch.setattr(run, 'write_summary', lambda a: summaries.append(True))
    run.run_mode(args, model, {}, mode, media, reference)
    out = args.output/'fake'/mode
    run.suite.verify_completed(out)
    assert len(list(out.glob('SYN*.result.json'))) == 84
    assert len(calls) == (84 if mode == 'full' else 42)
    assert all(frame == (1 if mode == 'full' else 0) for _, frame in calls)
    assert len(list(out.glob('SYN*.timing.json'))) == len(calls)
    if mode == 'gated':
        assert len((out/'pose-frames.jsonl').read_text().splitlines()) == 84
        untouched = json.loads((out/'SYN001.result.json').read_text())
        assert untouched['status'] == 'not_triggered' and untouched['prediction'] is None
    assert summaries == [True]
