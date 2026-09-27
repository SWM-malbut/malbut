"""Synthetic sequence contracts, not detection or identity accuracy metrics."""
import asyncio
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import verify_discovery_order as verification
from verify_discovery_order import MODES, trial


@pytest.mark.parametrize('mode', MODES)
def test_discovery_order_and_negative_controls(tmp_path, mode):
    result = asyncio.run(trial(mode, tmp_path / mode))
    positives = {'pose_first_direct', 'pose_first_deferred', 'cloud_first_deferred', 'old_answer'}
    assert result['linked'] == (mode in positives)
    assert result['journal_reopen_verified'] and result['real_api_calls'] == 0
    if result['linked']:
        assert result['person_cases'] == 1
        assert result['duplicate_growth'] == (0, 0, 0)
    expected = {
        'different_person': 'no_matching_track', 'ambiguous_people': 'ambiguous_tracks',
        'identity_switch': 'confirming_track', 'stale_track': 'current_target_unavailable',
        'camera_off': 'unknown_tracking_session',
        'candidate_during_cloud': 'incident_changed_during_scan',
    }
    if mode in expected:
        assert result['reason'] == expected[mode]
    if mode == 'pose_first_direct':
        assert result['scene_cases'] == 0 and result['manager_questions'] == 1
        assert result['call_purposes'] == ['incident', 'crosscheck']
    elif mode in {'pose_first_deferred', 'cloud_first_deferred'}:
        # Known remaining UX issue: a scene question and a person question
        # coexist. Do not assert that same-person linkage cancels the former.
        assert result['scene_cases'] == 1 and result['manager_questions'] == 2
    if mode == 'old_answer':
        assert result['obsolete_answer_rejected']
    if mode == 'candidate_during_cloud':
        assert result['person_cases'] == 1 and result['scene_cases'] == 0
        assert result['call_purposes'] == ['crosscheck', 'incident']
        assert result['manager_questions'] == 1


def test_cloud_first_then_pose_candidate_reuses_new_person_case(tmp_path):
    directory = tmp_path / 'cloud-first'
    asyncio.run(trial('cloud_first_deferred', directory))
    result = json.loads((directory / 'result.json').read_text())
    target = result['target_id']
    openings = [e for e in result['events'] if e['kind'] == 'incident_opened'
                and e['subject_key'] == 'P1']
    assert len(openings) == 1 and openings[0]['incident_id'] == target
    assert len([e for e in result['events'] if e['kind'] == 'cloud_discovery_linked']) == 1
    before, after = [p for p in result['phases'] if p['name'] in
                     {'before_duplicate_delivery', 'after_duplicate_delivery'}]
    assert before['incident_ids'] == after['incident_ids']
    assert before['questions'] == after['questions']
    assert before['fake_provider_calls'] == after['fake_provider_calls'] == 1


def test_existing_pose_case_does_not_change_after_deferred_link(tmp_path):
    directory = tmp_path / 'pose-first'
    asyncio.run(trial('pose_first_deferred', directory))
    result = json.loads((directory / 'result.json').read_text())
    first = result['phases'][0]
    assert first['name'] == 'pose_first_analysis'
    assert first['incident_ids'] == [result['target_id']]
    linked = next(e for e in result['events'] if e['kind'] == 'cloud_discovery_linked')
    assert linked['incident_id'] == result['target_id']
    assert result['linked']  # Final sample is already_linked, not a failure.


def test_cli_records_current_coordinator_and_preserves_outputs(tmp_path, monkeypatch):
    output = tmp_path / 'ordering'
    monkeypatch.setattr(sys, 'argv', ['verify_discovery_order', '--output', str(output)])
    verification.main()

    plan = verification.read(output / 'plan.json')
    completed = verification.read(output / 'completed.json')
    coordinator = (verification.REPO /
                   'malbut_fall_coordinator/malbut_fall_coordinator/fall_confirmation.py')
    assert plan['code'][str(coordinator)] == verification.digest(coordinator)
    assert completed['code'] == plan['code']
    assert not any('malbut_system_manager' in name for name in plan['code'])
    assert completed['complete'] and completed['new_api_calls'] == 0
    assert plan['modes'] == list(MODES)
    assert len(verification.read(output / 'summary.json')) == len(MODES)
    for name, expected in completed['files'].items():
        assert verification.digest(output / name) == expected

    before = {p: verification.digest(p) for p in output.rglob('*') if p.is_file()}
    with pytest.raises(ValueError, match='preserve outputs'):
        verification.main()
    assert {p: verification.digest(p) for p in output.rglob('*') if p.is_file()} == before
