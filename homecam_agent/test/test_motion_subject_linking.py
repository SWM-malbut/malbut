"""Unit safety checks only; fabricated boxes are not video accuracy evidence."""
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from experimental_visual_subject_bridge import VisualSubjectBridge, sample_schedule

B = [.1,.1,.4,.8]


def candidate(key='p1', token='t1', box=None, usable=True):
    return dict(id=key, token=token, box=box or B, usable=usable)


def confirmed():
    bridge = VisualSubjectBridge('scene')
    assert bridge.step(0, B, [candidate()])['active'] is None
    assert bridge.step(.25, B, [candidate()])['active'] is None
    assert bridge.step(.5, B, [candidate()])['active'] is not None
    return bridge


def test_samples_preserve_seed_without_using_gt():
    schedule = sample_schedule(123,24,2)
    assert schedule == [2]+list(range(12,123,6))
    schedule = sample_schedule(123,24,35)
    assert 35 in schedule and 36 not in schedule
    assert all(b-a >= 5 for a,b in zip(schedule,schedule[1:]))
    assert sample_schedule(123,24) == list(range(0,123,6))


@pytest.mark.parametrize('frames,fps,seed', [(0,24,0),(123,0,0),(123,24,123),(123,24,-1),(123,24,True)])
def test_invalid_schedule(frames,fps,seed):
    with pytest.raises(ValueError): sample_schedule(frames,fps,seed)


def test_confirmation_needs_count_and_elapsed_time():
    b = VisualSubjectBridge('scene')
    for t in (0,.1,.2): assert b.step(t,B,[candidate()])['active'] is None
    assert b.step(.5,B,[candidate()])['active'] is not None


def test_no_retroactive_assignment_or_duplicate_link_event():
    b = VisualSubjectBridge('scene')
    earlier = b.step(0,B,[candidate()])
    b.step(.25,B,[candidate()])
    assert b.step(.5,B,[candidate()])['events'][0]['event'] == 'linked'
    assert earlier['active'] is None
    assert b.step(.75,B,[candidate()])['events'] == []


@pytest.mark.parametrize('candidates,reason', [([], 'no_overlap'),
    ([candidate(usable=False,token=None)], 'pose_unusable'),
    ([candidate(),candidate('p2','t2')], 'ambiguous'),
    ([candidate(),candidate('unassigned',None,usable=False)], 'ambiguous')])
def test_loss_or_ambiguity_revokes_immediately(candidates,reason):
    b = confirmed()
    result = b.step(.75,B,candidates)
    assert result['active'] is None and result['reason'] == reason
    assert result['events'][0]['event'] == 'revoked'
    assert b.step(1,B,[candidate()])['active'] is None


@pytest.mark.parametrize('next_candidate', [candidate(token='t2'),candidate('p2','t2')])
def test_pose_token_or_id_rotation_needs_reconfirmation(next_candidate):
    b = confirmed()
    result = b.step(.75,B,[next_candidate])
    assert result['active'] is None
    assert result['events'][0]['reason'] == 'pose_identity_or_token_changed'
    assert b.step(1,B,[next_candidate])['active'] is None
    result = b.step(1.25,B,[next_candidate])
    assert result['active']['common_id'] == 'scene:v1'
    assert result['active']['pose_token'] == 't2'


def test_gap_rotates_visual_id():
    b = confirmed()
    result = b.step(1.1,B,[candidate()])
    assert result['common_id'] == 'scene:v2' and result['active'] is None


def test_visual_missing_does_not_copy_previous_box():
    b = confirmed()
    result = b.step(.75,None,[candidate()])
    assert result['common_id'] is None and result['active'] is None
    result = b.step(1,B,[candidate()])
    assert result['common_id'] == 'scene:v2' and result['active'] is None


def test_visual_jump_does_not_keep_same_identity():
    b = confirmed(); other = [.65,.1,.95,.8]
    result = b.step(.75,other,[candidate('p2','t2',other)])
    assert result['common_id'] == 'scene:v2' and result['active'] is None


@pytest.mark.parametrize('time', [.5,.4,float('nan'),float('inf')])
def test_old_or_invalid_time_does_not_extend_confirmation(time):
    b = confirmed()
    with pytest.raises(ValueError): b.step(time,B,[candidate()])
    assert b.step(.75,B,[candidate()])['active']['common_id'] == 'scene:v1'


def test_no_usable_token_no_association():
    b = VisualSubjectBridge('scene')
    with pytest.raises(ValueError): b.step(0,B,[candidate(token=None)])


def test_invalid_box_or_duplicate_id_is_rejected():
    b = VisualSubjectBridge('scene')
    with pytest.raises(ValueError): b.step(0,[-1,0,1,1],[])
    with pytest.raises(ValueError): b.step(0,B,[candidate(),candidate()])


def test_unusable_best_candidate_blocks_worse_usable_candidate():
    b = VisualSubjectBridge('scene')
    result = b.step(0,B,[candidate(box=[.1,.1,.3,.8]),candidate('weak',None,usable=False)])
    assert result['reason'] == 'pose_unusable' and result['active'] is None
