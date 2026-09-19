"""Exercise real report loading/writing with fake saved results, no provider calls."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import replay_vlm_frames as replay  # noqa: E402
import run_ollama_fall_suite as suite  # noqa: E402
import summarize_ollama_suite as detailed  # noqa: E402
import score_vlm_frames as standalone  # noqa: E402


def fixture(tmp_path, monkeypatch, completed=True, human_note=False):
    frozen, runs = tmp_path/'frozen', tmp_path/'runs'
    frozen.mkdir(); runs.mkdir()
    replay.save(frozen/'freeze.json', {})
    labels = [dict(case_id=f'SYN{i+1:03d}', label=label, source_path=f'neutral-{i}.mp4',
                   source_sha256=str(i)*64) for i, label in enumerate(
                       ('observed_fall','suspected_fall','normal_activity'))]
    if human_note:
        labels[2].update(judgment_evidence_limited=True,
                         judgment_note='속도 외 근거가 부족함 <사람 메모>')
    replay.save(frozen/'evaluation_labels.json', dict(
        classifications=dict(schema_version='malbut.synthetic-video-human-review.v2',
                             cases=labels, full_dataset_finalized=True),
        annotations=dict(cases=[dict(case_id=c['case_id']) for c in labels])))
    replay.save(frozen/'media.json', dict(cases=[dict(case_id=c['case_id']) for c in labels]))
    out = runs/'fake'/'full'; out.mkdir(parents=True)
    contract = dict(evaluation_version='v2', model=dict(name='fake'), mode='full',
                    thinking='disabled', freeze_sha256=replay.sha(frozen/'freeze.json'),
                    media_sha256=replay.sha(frozen/'media.json'),
                    case_order=[c['case_id'] for c in labels])
    replay.save(out/'run.json', dict(contract=contract, contract_sha256=replay.digest(contract)))
    for c in labels if completed else labels[:1]:
        cid = c['case_id']
        predicted = 'suspected_fall' if human_note and c == labels[2] else c['label']
        value = dict(outcome='classified', label=predicted, explanation_ko='테스트용 답변')
        raw = dict(done=True, done_reason='stop', message=dict(content=json.dumps(value)))
        replay.save(out/f'{cid}.input.json', dict(
            media_sha256=c['source_sha256'], contract_sha256=replay.digest(contract),
            frames=[dict(frame_index=0,timestamp_s=0)], duration_s=1, available_through_frame=0))
        replay.save(out/f'{cid}.response.json', raw)
        replay.save(out/f'{cid}.result.json', dict(
            case_id=cid, status='responded', valid=True, prediction=value,
            schema_errors=[],semantic_errors=[],request_s=1,
            contract_sha256=replay.digest(contract), response_sha256=replay.sha(out/f'{cid}.response.json')))
    if completed:
        replay.save(out/'completed.json', dict(
            run_sha256=replay.sha(out/'run.json'),
            files={p.name:replay.sha(p) for p in out.iterdir() if p.is_file()}))
    for module in (suite,detailed,standalone):
        monkeypatch.setattr(module,'verify_freeze',lambda *a:None)
    return frozen,runs,out


def test_suite_v2_report_uses_new_denominator_and_no_legacy_text(tmp_path, monkeypatch):
    frozen,runs,_ = fixture(tmp_path,monkeypatch)
    suite.report(SimpleNamespace(frozen=frozen,output=runs,evaluation_version='v2'))
    report = next(runs.glob('report-*.md')).read_text()
    assert '3/3 (100.0%)' in report
    assert '애매한 5개를 제외' not in report and '정상으로 처리한 시스템 지표' not in report
    assert 'gated: 미완료' in report


def test_detailed_v2_report_and_raw_json_do_not_use_legacy_audit(tmp_path,monkeypatch):
    frozen,runs,out = fixture(tmp_path,monkeypatch)
    before = {p:p.read_bytes() for p in out.iterdir()}
    dest=tmp_path/'report'
    detailed.run(SimpleNamespace(frozen=frozen,runs=[runs],output=dest))
    details=json.loads((dest/'details.json').read_text())['models'][0]
    assert details['measurements']['classification']['denominator']==3
    assert details['measurements']['checking']['caught']['denominator']==2
    assert details['final_json_supplement'] is None
    assert '3/3 (100.0%)' in (dest/'report.md').read_text()
    assert 'observed_fall' in (dest/'cases.csv').read_text(encoding='utf-8-sig')
    assert all(p.read_bytes()==data for p,data in before.items())


def test_partial_v2_run_has_no_completed_score(tmp_path,monkeypatch):
    frozen,runs,_=fixture(tmp_path,monkeypatch,completed=False)
    dest=tmp_path/'report'
    detailed.run(SimpleNamespace(frozen=frozen,runs=[runs],output=dest))
    details=json.loads((dest/'details.json').read_text())['models'][0]
    assert details['measurements'] is None
    assert '미완료 (1/3), 전체 점수 없음' in (dest/'report.md').read_text()
    suite.report(SimpleNamespace(frozen=frozen,output=runs,evaluation_version='v2'))
    assert 'v1 표' not in next(runs.glob('report-*.md')).read_text()


def test_standalone_loader_validates_v2_and_uses_all_three_labels(tmp_path,monkeypatch):
    frozen,_,out=fixture(tmp_path,monkeypatch)
    rows=standalone.load_rows(frozen,out)
    summary=standalone.summarize(rows)
    assert summary['classification']['numerator']==summary['classification']['denominator']==3
    assert '3/3 (100.0%)' in standalone.markdown(rows,summary)


def test_v2_preflight_rejects_changed_media_case_set(tmp_path,monkeypatch):
    frozen,_,_=fixture(tmp_path,monkeypatch)
    replay.check_evaluation_labels(frozen,'v2')
    (frozen/'media.json').write_text(json.dumps(dict(cases=[dict(case_id='unknown')])) )
    with pytest.raises(ValueError,match='case mismatch'):
        replay.check_evaluation_labels(frozen,'v2')


def test_all_reports_preserve_limited_normal_as_wrong_when_predicted_suspected(tmp_path, monkeypatch):
    frozen, runs, out = fixture(tmp_path, monkeypatch, human_note=True)
    before = {p: p.read_bytes() for p in out.iterdir()}
    suite.report(SimpleNamespace(frozen=frozen, output=runs, evaluation_version='v2'))
    suite_text = next(runs.glob('report-*.md')).read_text()
    dest = tmp_path/'noted-report'
    detailed.run(SimpleNamespace(frozen=frozen, runs=[runs], output=dest))
    rows = standalone.load_rows(frozen, out)
    result = standalone.summarize(rows)
    assert result['classification'] == dict(numerator=2, denominator=3, rate=2/3)
    assert result['checking']['unnecessary']['numerator'] == 1
    for report in (suite_text, (dest/'report.md').read_text(), standalone.markdown(rows, result)):
        assert '2/3 (66.7%)' in report
        assert '정답 정상 행동 → 예측 낙상 의심 · 오답' in report
        assert '속도 외 근거가 부족함 &lt;사람 메모&gt;' in report
    assert '속도 외 근거가 부족함' in (dest/'cases.csv').read_text(encoding='utf-8-sig')
    assert all(p.read_bytes() == data for p, data in before.items())


def test_malformed_judgment_note_blocks_before_provider(tmp_path, monkeypatch):
    frozen, _, _ = fixture(tmp_path, monkeypatch, human_note=True)
    path = frozen/'evaluation_labels.json'
    data = json.loads(path.read_text())
    data['classifications']['cases'][2]['judgment_note'] = ''
    path.write_text(json.dumps(data))
    monkeypatch.setattr(suite, 'api', lambda *a, **kw: pytest.fail('must not call provider'))
    with pytest.raises(ValueError, match='limited evidence requires a human note'):
        suite.execute(SimpleNamespace(frozen=frozen, evaluation_version='v2'))
