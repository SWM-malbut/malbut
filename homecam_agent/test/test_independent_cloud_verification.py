"""Saved-input CLI regression tests; synthetic observations, no model or ROS."""

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import replay_independent_cloud_verification as verification
from test_reviewed_pose_cloud_replay import fixture


def prepare_inputs(tmp_path, *, pose_present):
    rows, _, record, result, _, meta = fixture()
    if not pose_present:
        for row in rows:
            row['candidate_payload']['tracks'] = []
            row['candidate_payload']['candidates'] = []
    baseline, audit, spatial = [tmp_path / name for name in ('baseline', 'audit', 'spatial')]
    for directory in (baseline, audit, spatial, tmp_path / 'provider/results',
                      tmp_path / 'provider/inputs'):
        directory.mkdir(parents=True)
    result_path = tmp_path / 'provider/results/synthetic.json'
    input_path = tmp_path / 'provider/inputs/synthetic.input.json'
    config_path = verification.REPO / 'malbut_agent_server/config/fall_runtime.example.json'
    media_path = spatial / 'media.json'
    verification.save(result_path, result)
    verification.save(input_path, record)
    verification.save(media_path, {'cases': [dict(meta, case_id='synthetic')]})
    verification.save(baseline / 'synthetic.pose.json', rows)
    verification.save(baseline / 'plan.json', {'cases': ['synthetic']})
    verification.save(baseline / 'provenance.json', {
        'sources': {str(p): verification.digest(p)
                    for p in (result_path, input_path, config_path, media_path)}})
    verification.save(baseline / 'completed.json', {
        'files': {p.name: verification.digest(p) for p in baseline.iterdir()}})
    verification.save(audit / 'cases.json', {
        'fixture': [{'case_id': 'synthetic', 'result_path': str(result_path)}]})
    return baseline, audit, spatial, result_path


def set_arguments(monkeypatch, baseline, audit, spatial, output):
    monkeypatch.setattr(sys, 'argv', [
        'replay_independent_cloud_verification',
        '--baseline', str(baseline), '--audit', str(audit),
        '--spatial', str(spatial), '--output', str(output)])


@pytest.mark.parametrize('pose_present', [True, False])
def test_cli_replays_questions_and_hashes_new_coordinator(tmp_path, monkeypatch, pose_present):
    baseline, audit, spatial, _ = prepare_inputs(tmp_path, pose_present=pose_present)
    before = {p: verification.digest(p) for p in tmp_path.rglob('*') if p.is_file()}
    output = tmp_path / 'replay'
    set_arguments(monkeypatch, baseline, audit, spatial, output)
    verification.main()

    row, = verification.read(output / 'summary.json')
    assert row['reply_usable'] and row['person_linked'] == pose_present
    assert row['subject_questions'] == int(pose_present)
    assert row['scene_questions'] == int(not pose_present)
    assert row['repeat_questions'] == 0 and row['repeat_incidents_unchanged']
    assert row['journal_reopen_verified'] and row['new_api_calls'] == 0

    provenance = verification.read(output / 'provenance.json')
    folder = verification.REPO / 'malbut_fall_coordinator/malbut_fall_coordinator'
    assert provenance['new_api_calls'] == 0
    assert not any('malbut_system_manager' in name for name in provenance['sources'])
    for path in folder.rglob('*.py'):
        assert provenance['sources'][str(path)] == verification.digest(path)
    for name, expected in provenance['sources'].items():
        assert verification.digest(name) == expected
    completed = verification.read(output / 'completed.json')
    for name, expected in completed['files'].items():
        assert verification.digest(output / name) == expected
    assert all(verification.digest(p) == expected for p, expected in before.items())

    frozen_output = {p: verification.digest(p) for p in output.iterdir()}
    with pytest.raises(ValueError, match='preserve previous results'):
        verification.main()
    assert {p: verification.digest(p) for p in output.iterdir()} == frozen_output


@pytest.mark.parametrize('changed', ['saved_response', 'baseline_pose'])
def test_cli_still_rejects_changed_frozen_inputs(tmp_path, monkeypatch, changed):
    baseline, audit, spatial, result_path = prepare_inputs(tmp_path, pose_present=True)
    changed_path = (result_path if changed == 'saved_response'
                    else baseline / 'synthetic.pose.json')
    changed_path.write_text('{}')
    output = tmp_path / 'replay'
    set_arguments(monkeypatch, baseline, audit, spatial, output)
    with pytest.raises(ValueError, match='(original replay input|baseline result) changed'):
        verification.main()
    assert not (output / 'completed.json').exists()
