"""A weak verification request never upgrades uncertainty to observed low posture."""
import copy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from experimental_pose_disagreement import PoseDisagreementConfig, PoseDisagreementExperiment
from replay_pose_stability import replay_rows
from test_pose_stability import row, step


def prepared():
    exp = PoseDisagreementExperiment()
    for f in (0, 2, 4):
        assert not step(exp, row(f))[0]
    return exp


def test_request_keeps_past_low_and_current_uncertainty_separate():
    exp = prepared()
    assert not step(exp, row(6, 'disagreement'))[0]
    out, _ = step(exp, row(8, 'disagreement'))
    c, = out
    assert c['candidateKind'] == 'found_down' and c['requiresVerification']
    assert c['observationAvailability'] == 'observed' and c['observationId'] == 'frame-8'
    assert c['evidenceEndSec'] == .8 and not c['evidence']['pose']['horizontal']
    temporal = c['evidence']['temporal']
    assert temporal['observedLowSpanSec'] == pytest.approx(.4)
    assert temporal['lastLowTimestampSec'] == .4
    assert [s['frame'] for s in temporal['lowSamples']] == [0, 2, 4]
    assert [s['frame'] for s in temporal['disagreementSamples']] == [6, 8]
    assert temporal['currentPoseObserved'] and not temporal['currentLowPostureConfirmed']
    assert 'current_low_posture_unconfirmed' in c['uncertainties']
    assert not step(exp, row(9, 'disagreement'))[0]


@pytest.mark.parametrize('frames', [(0, 2), (0, 1, 2)])
def test_disagreement_does_not_complete_inadequate_low_history(frames):
    exp = PoseDisagreementExperiment()
    for f in frames:
        step(exp, row(f))
    for f in (4, 6):
        assert not step(exp, row(f, 'disagreement'))[0]


@pytest.mark.parametrize('problem', ['missing', 'ambiguous', 'upright', 'poor', 'unassigned',
                                     'new_anchor', 'new_id', 'stale', 'size', 'clock', 'depth'])
def test_invalid_continuity_cannot_trigger_from_old_evidence(problem):
    exp = prepared()
    r, size = row(6, 'disagreement'), (640, 400)
    d = r['fall_analysis']['tracks'][0]
    if problem == 'missing':
        r['observations'] = []
        d.update(features=None, trackingState='missing')
    elif problem == 'ambiguous':
        d['trackingState'] = 'ambiguous'
    elif problem == 'upright':
        r = row(6, 'upright')
    elif problem == 'poor':
        d['features']['usable'] = False
    elif problem == 'unassigned':
        r['fall_analysis']['unassignedCount'] = 1
    elif problem == 'new_anchor':
        d['features']['anchor_names'] = ['left_hip', 'left_shoulder']
    elif problem == 'new_id':
        r = row(6, 'disagreement', 'b')
    elif problem == 'stale':
        r = row(10, 'disagreement')
    elif problem == 'size':
        size = (800, 600)
    elif problem == 'clock':
        r['timestamp_s'] = .4
    else:
        d['features'].update(floor_height_m=1., near_floor=False)
    assert not step(exp, r, size)[0]
    assert not step(exp, row(8, 'disagreement'), size)[0]
    assert not step(exp, row(10, 'disagreement'), size)[0]


def test_current_pose_return_starts_a_new_history_not_previous_experiment():
    exp = prepared()
    step(exp, row(6, 'disagreement'))
    step(exp, row(8))
    assert not step(exp, row(10, 'disagreement'))[0]
    assert not step(exp, row(12, 'disagreement'))[0]


def test_stale_second_disagreement_never_triggers():
    exp = prepared()
    step(exp, row(8, 'disagreement'))
    assert not step(exp, row(10, 'disagreement'))[0]


def test_no_extra_request_for_low_alone_or_disagreement_alone():
    for kind in ('low', 'disagreement', 'upright'):
        exp = PoseDisagreementExperiment()
        for f in range(0, 30, 2):
            assert not step(exp, row(f, kind))[0]


def test_duplicate_observation_and_malformed_clock_fail():
    exp = prepared()
    r = row(6, 'disagreement')
    r['observations'].append(copy.deepcopy(r['observations'][0]))
    with pytest.raises(ValueError):
        step(exp, r)
    r = row(6)
    r['timestamp_s'] = float('nan')
    with pytest.raises(ValueError):
        step(exp, r)


def test_already_requested_person_receives_evidence_update_not_an_extra_call():
    rows = [row(f, 'low' if f <= 4 else 'disagreement') for f in (0, 2, 4, 6, 8)]
    original = dict(candidateId='original', targetTrackId='a', candidateKind='fall_suspected',
                    revision=1, requiresVerification=True)
    rows[2]['fall_analysis']['candidates'] = [original]
    out = list(replay_rows(rows, {'X': dict(width=640, height=400)},
                          PoseDisagreementConfig(), True, 'disagreement_request'))
    assert [c for r in out for c in r['fall_analysis']['candidates']] == [original]
    u, = [u for r in out for u in r['verification_updates']]
    assert u['evidence']['candidateKind'] == 'found_down'
    assert not u['evidence']['evidence']['temporal']['currentLowPostureConfirmed']


@pytest.mark.parametrize('kw', [dict(window_sec=True), dict(maximum_after_low_sec=3),
    dict(minimum_low_samples=2.5), dict(minimum_disagreement_samples=1),
    dict(minimum_low_span_sec=float('nan')), dict(window_sec=-1)])
def test_invalid_configuration_fails(kw):
    with pytest.raises(ValueError):
        PoseDisagreementConfig(**kw)
