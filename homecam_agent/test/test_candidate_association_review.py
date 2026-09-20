"""Review bookkeeping never promotes sparse box hints to person identity proof."""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import review_candidate_associations as review


def fixture_run(tmp_path, monkeypatch):
    frozen, run = tmp_path / 'frozen', tmp_path / 'run'
    frozen.mkdir()
    run.mkdir()
    case = dict(case_id='X', persons=[])
    meta = dict(case_id='X', frames=2, fps=5)
    labels = dict(annotations=dict(cases=[case]), classifications=dict(cases=[
        dict(case_id='X', label='suspected_fall', review_case_id='V001')]))
    review.write_json(frozen / 'media.json', dict(cases=[meta]))
    review.write_json(frozen / 'evaluation_labels.json', labels)
    review.write_json(frozen / 'freeze.json', dict(match={}, files={
        name: review.sha(frozen / name)
        for name in ('media.json', 'evaluation_labels.json')}))
    metadata = dict(freeze_sha256=review.sha(frozen / 'freeze.json'),
                    sample_fps=5, fall_config_sha256='config')
    review.write_json(run / 'run.json', metadata)
    rows = [dict(case_id='X', frame_index=i, timestamp_s=i / 5,
                 pose_ms=1, pipeline_ms=2,
                 fall_analysis=dict(status='ok', configSha256='config'))
            for i in range(2)]
    (run / 'frames.jsonl').write_text('\n'.join(json.dumps(r) for r in rows))
    review.write_json(run / 'completed.json', dict(
        frames_sha256=review.sha(run / 'frames.jsonl'),
        run_sha256=review.sha(run / 'run.json')))
    event = dict(case_id='X', key='event-X', candidate=dict(targetTrackId='track-A'))
    monkeypatch.setattr(review, 'events', lambda rows: [event])
    monkeypatch.setattr(review, 'anchors', lambda *args: [
        dict(frame_index=0, track_id='track-A', status='matched'),
        dict(frame_index=1, track_id='track-B', status='matched')])
    return frozen, run


def test_sparse_matches_are_only_hints_not_final_association(tmp_path, monkeypatch):
    frozen, run = fixture_run(tmp_path, monkeypatch)
    files = [*frozen.iterdir(), *run.iterdir()]
    before = {p: review.sha(p) for p in files}
    *_, records = review.prepare(frozen, run)
    assert len(records) == 1
    assert records[0]['association'] == 'unknown'
    assert records[0]['note'] == 'RGB review not yet completed'
    assert records[0]['sparse_support'] == [
        dict(frame_index=0, track_id='track-A', status='matched')]
    assert {p: review.sha(p) for p in files} == before


@pytest.mark.parametrize('name', ['run.json', 'frames.jsonl'])
def test_changed_run_cannot_be_used_for_review(tmp_path, monkeypatch, name):
    frozen, run = fixture_run(tmp_path, monkeypatch)
    with (run / name).open('a') as stream:
        stream.write(' ')
    with pytest.raises(ValueError, match='run changed'):
        review.prepare(frozen, run)


def test_changed_labels_are_rejected(tmp_path, monkeypatch):
    frozen, run = fixture_run(tmp_path, monkeypatch)
    with (frozen / 'evaluation_labels.json').open('a') as stream:
        stream.write(' ')
    with pytest.raises(ValueError):
        review.prepare(frozen, run)


def test_empty_candidates_do_not_create_review_events(tmp_path, monkeypatch):
    frozen, run = fixture_run(tmp_path, monkeypatch)
    monkeypatch.setattr(review, 'events', lambda rows: [])
    *_, records = review.prepare(frozen, run)
    assert records == []


def test_missing_sample_cannot_be_hidden_by_review(tmp_path, monkeypatch):
    frozen, run = fixture_run(tmp_path, monkeypatch)
    first = (run / 'frames.jsonl').read_text().splitlines()[0]
    (run / 'frames.jsonl').write_text(first)
    completed = json.loads((run / 'completed.json').read_text())
    completed['frames_sha256'] = review.sha(run / 'frames.jsonl')
    (run / 'completed.json').write_text(json.dumps(completed))
    with pytest.raises(ValueError):
        review.prepare(frozen, run)
