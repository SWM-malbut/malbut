"""Offline ROI geometry, bounded scheduling and provenance; not detection accuracy."""
from dataclasses import replace
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import replay_fall_baseline  # noqa: E402,F401
from experimental_roi_pose import (  # noqa: E402
    RoiPlanner, RoiPoseConfig, crop_rectangle, fuse_poses, project_pose,
)
from homecam_detector.pose import PersonPose, PoseKeypoint  # noqa: E402
from replay_roi_pose import same_json  # noqa: E402


def pose(box=(.4, .2, .6, .8), score=.8):
    return PersonPose(score, box, (PoseKeypoint('nose', .5, .3, .9),), 1)


def test_stored_contract_comparison_accepts_arrays_but_not_changed_values():
    assert same_json({'anchors': ('left_hip',)}, {'anchors': ['left_hip']})
    assert not same_json({'score': .1}, {'score': .11})
    assert not same_json({'track': 'a'}, {'track': 'b'})


def row(tick, ids=('a',), score=.8, usable=True, state='tracked', unknown=False,
        status='ok'):
    observations = [dict(observation_index=i, track_id=tid,
                         pose=pose(score=score).as_dict(), features={'usable': usable})
                    for i, tid in enumerate(ids)] if state != 'missing' else []
    if unknown:
        observations.append(dict(observation_index=len(observations), track_id=None,
                                 pose=pose().as_dict(), features={'usable': True}))
    return dict(frame_index=tick*2, timestamp_s=tick*.2, observations=observations,
                baseline_analysis=dict(status=status, unassignedCount=int(unknown), tracks=[
                    dict(targetTrackId=tid, trackingState=state) for tid in ids]))


def seed(planner, ids=('a',)):
    for tick in range(3):
        assert planner.update(row(tick, ids), (640, 400))[0] == []


def test_crop_uses_actual_dimensions_and_clips_to_image_without_padding():
    cfg = RoiPoseConfig()
    # Seed is 40 x 80 pixels. Expanded width=160, height=160, shifted down 20.
    assert crop_rectangle((.4, .2, .5, .6), (400, 200), cfg) == (100, 20, 260, 180)
    assert crop_rectangle((0., 0., 1., 1.), (640, 400), cfg) == (0, 0, 640, 400)
    assert crop_rectangle((0., 0., .01, .01), (640, 400), cfg) == (0, 0, 52, 51)


@pytest.mark.parametrize('box,size', [
    ((0., 0., 0., 1.), (640, 400)), ((-.1, 0., .5, 1.), (640, 400)),
    ((0., 0., float('nan'), 1.), (640, 400)), ((0., 0., .5, 1.), (0, 400)),
])
def test_invalid_crop_geometry_rejected(box, size):
    with pytest.raises(ValueError):
        crop_rectangle(box, size, RoiPoseConfig())


def test_projection_returns_actual_global_geometry_without_changing_confidence():
    p = pose((0., 0., 1., 1.))
    mapped = project_pose(p, (100, 20, 300, 180), (400, 200))
    assert mapped.box == (.25, .1, .75, .9)
    assert mapped.keypoints[0].x == .5
    assert mapped.keypoints[0].y == pytest.approx(.34)
    assert mapped.box_confidence == p.box_confidence
    assert mapped.keypoints[0].confidence == .9 and mapped.visible_keypoints == 1
    assert p.box == (0., 0., 1., 1.)  # immutable input


@pytest.mark.parametrize('rectangle', [(-1, 0, 100, 100), (0, 0, 641, 400),
                                       (0, 0, 0, 10), (0., 0, 100, 100)])
def test_invalid_projection_rectangle_rejected(rectangle):
    with pytest.raises(ValueError):
        project_pose(pose(), rectangle, (640, 400))


def test_fusion_preserves_weak_full_pose_and_distinct_overlapping_people():
    full = pose(score=.1)
    duplicate = replace(full, box_confidence=.99)
    different = pose((.45, .3, .65, .7), score=.11)
    obs = [dict(pose=p, source='roi', seed_track_id='old-a')
           for p in (duplicate, different)]
    fused, provenance, raw = fuse_poses((full,), obs, RoiPoseConfig())
    assert fused == (full, different) and fused[0] is full
    assert provenance[0] == {'source': 'full_frame'}
    assert raw[0]['duplicate_of'] == 0 and raw[1]['duplicate_of'] is None
    assert len(raw) == 2  # even discarded duplicate recorded
    assert not hasattr(fused[1], 'track_id')  # seed does not force identity


def test_duplicates_across_rois_are_recorded_once_but_keep_raw_evidence():
    p = pose()
    observations = [dict(pose=p, source='roi', roi_index=i) for i in (0, 1)]
    fused, _, raw = fuse_poses((), observations, RoiPoseConfig())
    assert fused == (p,)
    assert [r['duplicate_of'] for r in raw] == [None, 0]


def test_seed_requires_three_consecutive_strong_usable_full_frame_observations():
    planner = RoiPlanner()
    for tick, score, usable in ((0, .8, True), (1, .44, True), (2, .8, True),
                                (3, .8, False), (4, .8, True), (5, .8, True)):
        planner.update(row(tick, score=score, usable=usable), (640, 400))
        assert not planner.seeds
    planner.update(row(6), (640, 400))
    plans, _ = planner.update(row(7, state='missing'), (640, 400))
    assert len(plans) == 1 and plans[0]['seed_frame_index'] == 12


def test_weak_but_usable_current_pose_does_not_trigger_extra_compute_or_refresh_seed():
    planner = RoiPlanner()
    seed(planner)
    assert planner.update(row(3, score=.1), (640, 400))[0] == []
    assert planner.seeds['a']['seed_timestamp_s'] == .4
    assert planner.update(row(4, score=.1, usable=False), (640, 400))[0]


def test_roi_attempts_do_not_extend_seed_ttl_or_invent_a_new_seed():
    planner = RoiPlanner()
    seed(planner)
    for tick in range(3, 10):
        plans, _ = planner.update(row(tick, state='missing'), (640, 400))
        assert len(plans) == 1
        assert plans[0]['seed_timestamp_s'] == .4
    assert planner.update(row(10, state='missing'), (640, 400))[0] == []
    assert not planner.seeds


@pytest.mark.parametrize('kind', ['ambiguous', 'unassigned', 'failed'])
def test_ambiguous_or_failed_full_frame_does_not_start_crop(kind):
    planner = RoiPlanner()
    seed(planner)
    r = row(3, state='ambiguous' if kind == 'ambiguous' else 'missing',
            unknown=(kind == 'unassigned'), status='failed' if kind == 'failed' else 'ok')
    assert planner.update(r, (640, 400))[0] == []


def test_lost_id_can_expire_before_bounded_seed_but_not_refresh_it():
    planner = RoiPlanner()
    seed(planner)
    plans, _ = planner.update(row(3, ids=()), (640, 400))
    assert len(plans) == 1 and plans[0]['seed_track_id'] == 'a'
    assert planner.streaks == {}


@pytest.mark.parametrize('change', ['clock_gap', 'size', 'backwards'])
def test_source_discontinuity_discards_seed_or_rejects_clock(change):
    planner = RoiPlanner()
    seed(planner)
    if change == 'backwards':
        with pytest.raises(ValueError):
            planner.update(row(2, state='missing'), (640, 400))
    else:
        r = row(5 if change == 'clock_gap' else 3, state='missing')
        assert not planner.update(r, (800, 600) if change == 'size' else (640, 400))[0]
        assert not planner.seeds


def test_maximum_two_crops_rotate_among_three_missing_people():
    planner = RoiPlanner()
    ids = ('a', 'b', 'c')
    seed(planner, ids)
    first = planner.update(row(3, ids, state='missing'), (640, 400))[0]
    second = planner.update(row(4, ids, state='missing'), (640, 400))[0]
    assert len(first) == len(second) == 2
    assert second[0]['seed_track_id'] == 'c'


@pytest.mark.parametrize('settings', [
    {'maximum_rois_per_frame': 0}, {'minimum_seed_samples': 1},
    {'seed_ttl_sec': float('inf')}, {'seed_confidence': 1.1},
    {'minimum_side_px': 1.5}, {'maximum_rois_per_frame': True},
])
def test_invalid_configuration_rejected(settings):
    with pytest.raises(ValueError):
        RoiPoseConfig(**settings)
