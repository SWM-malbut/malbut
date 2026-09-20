"""Deterministic geometry/temporal contracts, not measured fall accuracy."""

from dataclasses import replace
import json
import math

import pytest

from homecam_detector.fall_candidate import (
    FallCandidateConfig, FallCandidateDetector, extract_pose_features,
)
from homecam_detector.pose import PersonPose, PoseKeypoint
from homecam_detector.pose_tracker import PoseTrackingResult, TrackedPose, UnassignedPose


def body(posture="upright", score=0.8, point_score=0.9, offset=(0, 0)):
    """Pixel-space anatomy in a 640x400 image."""
    if posture == "upright":
        box = (220, 60, 340, 360)
        joints = ((260, 100), (300, 100), (264, 220), (296, 220),
                  (265, 280), (295, 280), (266, 345), (294, 345))
    elif posture == "lying":
        box = (190, 250, 570, 350)
        joints = ((235, 282), (235, 315), (365, 282), (365, 315),
                  (455, 284), (455, 317), (550, 286), (550, 320))
    elif posture == "bent":
        box = (190, 130, 460, 360)
        joints = ((420, 160), (420, 180), (300, 160), (300, 180),
                  (300, 260), (320, 260), (300, 340), (320, 340))
    else:
        raise ValueError(posture)
    names = [f"{side}_{joint}" for joint in ("shoulder", "hip", "knee", "ankle")
             for side in ("left", "right")]
    dx, dy = offset
    return PersonPose(score, ((box[0]+dx)/640, (box[1]+dy)/400,
                              (box[2]+dx)/640, (box[3]+dy)/400), tuple(
        PoseKeypoint(name, (x+dx)/640, (y+dy)/400, point_score)
        for name, (x, y) in zip(names, joints)
    ), len(names) if point_score >= 0.5 else 0)


def tracked(pose, identity="person-a", state="tracked"):
    return TrackedPose(identity, state, pose,
                       "strong" if pose and pose.box_confidence >= 0.45 else "weak",
                       100, 100, 0, ())


def step(detector, poses, stamp, motion="stationary", depth=None):
    tracks = tuple(tracked(p, name) for name, p in poses.items())
    return detector.update(PoseTrackingResult(tracks, (), ()), capture_time=stamp,
                           image_size=(640, 400), robot_motion=motion, depth_by_track=depth)


def low_sequence(detector, score=0.8, motion="stationary", depth=None):
    results = [step(detector, {"a": body("lying", score)}, n * 0.2, motion, depth)
               for n in range(5)]
    return [c for r in results for c in r["candidates"]]


def test_id_or_repeated_low_score_box_is_not_fall_evidence():
    detector = FallCandidateDetector()
    empty = replace(body("lying", score=0.1), keypoints=(), visible_keypoints=0)
    for n in range(20):
        output = step(detector, {"background": empty}, n * 0.2)
        assert output["candidates"] == []
        assert output["tracks"][0]["status"] == "insufficient_pose"
    assert "normal" not in json.dumps(output)


def test_stable_upright_subject_check_is_separate_from_incident_resolution():
    detector = FallCandidateDetector()
    for n in range(11):
        output = step(detector, {"a": body()}, n * 0.2)
        check = output["tracks"][0]["subjectCheck"]
        assert check["state"] == ("clear" if n == 10 else "unknown")
    assert check["reason"] == "stable_upright"
    assert output["subjectCheckVersion"] == 1
    assert output["tracks"][0]["box"] == list(body().box)
    assert not output["candidates"]
    assert "resolved" not in json.dumps(output)


@pytest.mark.parametrize("condition", ["weak", "moving", "unknown", "partial", "lying"])
def test_incomplete_or_low_observations_never_generate_clear(condition):
    detector = FallCandidateDetector()
    pose = body("lying" if condition == "lying" else "upright",
                score=0.2 if condition == "weak" else 0.8)
    if condition == "partial":
        pose = replace(pose, keypoints=tuple(p for p in pose.keypoints
                                           if not p.name.endswith("ankle")))
    for n in range(20):
        output = step(detector, {"a": pose}, n * 0.2,
                      condition if condition in {"moving", "unknown"} else "stationary")
        assert output["tracks"][0]["subjectCheck"]["state"] != "clear"


def test_gap_and_lost_track_restart_subject_clear_duration():
    detector = FallCandidateDetector()
    for n in range(11):
        output = step(detector, {"a": body()}, n * 0.2)
    assert output["tracks"][0]["subjectCheck"]["state"] == "clear"
    output = step(detector, {"a": body()}, 3.0)
    assert output["tracks"][0]["subjectCheck"]["state"] == "unknown"
    detector.unavailable("inference_error")
    output = step(detector, {"a": body()}, 3.2)
    assert output["tracks"][0]["subjectCheck"]["state"] == "unknown"


@pytest.mark.parametrize("score", [0.1, 0.38, 0.9])
@pytest.mark.parametrize("motion", ["stationary", "moving", "unknown"])
def test_still_lying_person_is_candidate_without_prior_strong_detection(score, motion):
    candidates = low_sequence(FallCandidateDetector(), score=score, motion=motion)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["candidateKind"] == "found_down"
    assert candidate["requiresVerification"] is True
    assert candidate["evidenceStartSec"] == 0
    assert candidate["evidenceEndSec"] == pytest.approx(0.6)
    assert "lying_surface_unknown" in candidate["uncertainties"]
    assert ("weak_pose_detection" in candidate["uncertainties"]) == (score < 0.45)


@pytest.mark.parametrize("motion", ["stationary", "moving", "unknown"])
def test_rapid_posture_change_is_separate_from_found_down(motion):
    detector = FallCandidateDetector()
    step(detector, {"a": body()}, 0, motion)
    step(detector, {"a": body()}, 0.2, motion)
    assert not step(detector, {"a": body("lying")}, 0.4, motion)["candidates"]
    output = step(detector, {"a": body("lying")}, 0.6, motion)
    candidate = output["candidates"][0]
    assert candidate["candidateKind"] == "fall_suspected"
    temporal = candidate["evidence"]["temporal"]
    assert temporal["tiltChangeDeg"] == 90
    assert (temporal["descentBodyLengths"] is None) == (motion != "stationary")
    assert not step(detector, {"a": body("lying")}, 0.8, motion)["candidates"]


def test_different_people_do_not_form_one_false_posture_transition():
    detector = FallCandidateDetector()
    step(detector, {"helper": body()}, 0)
    outputs = [step(detector, {"helper": body(), "lying": body("lying")}, 0.2+n*0.2)
               for n in range(5)]
    candidates = [c for r in outputs for c in r["candidates"]]
    assert len(candidates) == 1
    assert candidates[0]["targetTrackId"] == "lying"
    assert candidates[0]["candidateKind"] == "found_down"


@pytest.mark.parametrize("motion", ["stationary", "moving", "unknown"])
def test_screen_translation_alone_does_not_create_fall_candidate(motion):
    detector = FallCandidateDetector()
    for n in range(5):
        output = step(detector, {"a": body(offset=(n*10, n*5))}, n*0.2, motion)
        assert not output["candidates"]


def test_bending_with_feet_below_body_is_not_lying_candidate():
    detector = FallCandidateDetector()
    for n in range(6):
        output = step(detector, {"a": body("bent")}, n*0.2)
        assert not output["candidates"]
        assert not output["tracks"][0]["features"]["horizontal"]


@pytest.mark.parametrize("interrupt", ["missing", "ambiguous", "poor_pose", "error", "gap"])
def test_unobserved_interval_cannot_be_rewritten_as_witnessed_fall(interrupt):
    detector = FallCandidateDetector()
    step(detector, {"a": body()}, 0)
    if interrupt in ("missing", "ambiguous"):
        detector.update(PoseTrackingResult((tracked(None, "a", interrupt),), (), ()),
                        capture_time=0.2, image_size=(640, 400), robot_motion="stationary")
    elif interrupt == "poor_pose":
        step(detector, {"a": body(point_score=0.1)}, 0.2)
    elif interrupt == "error":
        assert detector.unavailable("inference_error")["status"] == "inference_error"
    start = 1.0 if interrupt == "gap" else 0.4
    outputs = [step(detector, {"a": body("lying")}, start+n*0.2) for n in range(6)]
    candidates = [c for r in outputs for c in r["candidates"]]
    assert [c["candidateKind"] for c in candidates] == ["found_down"]


def depth(height=0.1):
    return dict(usable=True, alignedToRgb=True, stale=False, validTorsoRatio=1.0,
                sampledTorsoPoints=4, torsoFloorDistanceM=height)


def test_depth_supports_low_torso_without_horizontal_image_geometry():
    detector = FallCandidateDetector()
    outputs = [step(detector, {"a": body()}, n*0.2, depth={"a": depth()}) for n in range(5)]
    candidate = [c for r in outputs for c in r["candidates"]][0]
    assert candidate["candidateKind"] == "found_down"
    assert "measured_torso_near_floor" in candidate["reasons"]


def test_usable_depth_above_floor_prevents_horizontal_only_found_down():
    assert low_sequence(FallCandidateDetector(), depth={"a": depth(1.0)}) == []


@pytest.mark.parametrize("bad", [
    {"stale": True}, {"alignedToRgb": False}, {"usable": False},
    {"torsoFloorDistanceM": -0.1}, {"torsoFloorDistanceM": float("nan")},
    {"torsoFloorDistanceM": float("inf")}, {"sampledTorsoPoints": 1},
    {"validTorsoRatio": 0.1},
])
def test_bad_depth_never_supplies_false_floor_evidence(bad):
    config = FallCandidateConfig()
    value = depth()
    value.update(bad)
    features = extract_pose_features(body(), (640, 400), value, config)
    assert features.floor_height_m is None and not features.near_floor


def test_source_aspect_ratio_used_for_angles_not_stretched_normalized_coordinates():
    points = tuple(PoseKeypoint(name, x, y, 0.9) for name, x, y in (
        ("left_shoulder", 0.2, 0.2), ("right_shoulder", 0.22, 0.2),
        ("left_hip", 0.4, 0.4), ("right_hip", 0.42, 0.4),
    ))
    pose = PersonPose(0.9, (0.1, 0.1, 0.7, 0.7), points, 4)
    features = extract_pose_features(pose, (800, 400), {}, FallCandidateConfig())
    assert features.torso_angle_deg == pytest.approx(math.degrees(math.atan2(2, 1)))


def test_no_frame_count_shortcut_or_duplicate_camera_stamp():
    detector = FallCandidateDetector()
    for n in range(10):
        assert not step(detector, {"a": body("lying")}, n*0.01)["candidates"]
    repeated = step(detector, {"a": body("lying")}, 0.09)
    assert repeated["status"] == "invalid_capture_time"
    assert not repeated["candidates"]


def test_repeated_lying_deduplicates_but_later_new_fall_can_emit():
    detector = FallCandidateDetector()
    first = low_sequence(detector)[0]
    for n in range(5, 30):
        assert not step(detector, {"a": body("lying")}, n*0.2)["candidates"]
    # Rearming is only local candidate emission; no incident "resolved" output.
    for n in range(30, 43):
        output = step(detector, {"a": body()}, n*0.2)
        assert not output["candidates"]
    step(detector, {"a": body("lying")}, 8.6)
    new = step(detector, {"a": body("lying")}, 8.8)["candidates"][0]
    assert new["candidateId"] != first["candidateId"]
    assert new["candidateKind"] == "fall_suspected"


def test_expiry_or_reset_does_not_reuse_candidate_id_or_assert_recovery():
    detector = FallCandidateDetector()
    first = low_sequence(detector)[0]
    output = detector.update(PoseTrackingResult((), (), ("a",)), capture_time=1,
                             image_size=(640, 400), robot_motion="stationary")
    assert output["expiredTrackIds"] == ["a"]
    assert output["candidates"] == []
    detector.reset()
    new = low_sequence(detector)[0]
    assert new["candidateId"] != first["candidateId"]


def test_later_transition_adds_revision_without_second_candidate_identity():
    detector = FallCandidateDetector()
    first = low_sequence(detector)[0]
    step(detector, {"a": body()}, 1.0)
    step(detector, {"a": body()}, 1.2)
    step(detector, {"a": body("lying")}, 1.4)
    updated = step(detector, {"a": body("lying")}, 1.6)["candidates"][0]
    assert updated["candidateId"] == first["candidateId"]
    assert updated["revision"] == 2
    assert updated["candidateKind"] == "fall_suspected"
    assert updated["evidenceStartSec"] == first["evidenceStartSec"]


def test_lost_pose_never_clears_an_existing_candidate():
    detector = FallCandidateDetector()
    first = low_sequence(detector)[0]
    poor = step(detector, {"a": body(point_score=0.1)}, 1.0)
    assert poor["tracks"][0]["status"] == "insufficient_pose"
    assert poor["tracks"][0]["activeCandidateId"] == first["candidateId"]
    assert poor["candidates"] == []


def test_unassigned_pose_is_reported_but_not_attached_to_another_person():
    detector = FallCandidateDetector()
    result = PoseTrackingResult((), (UnassignedPose(body("lying"), "association_ambiguous"),), ())
    output = detector.update(result, capture_time=0, image_size=(640, 400),
                             robot_motion="stationary")
    assert output["unassignedCount"] == 1
    assert not output["candidates"]


@pytest.mark.parametrize("kwargs", [
    {"temporal_window_sec": 0}, {"temporal_window_sec": float("nan")},
    {"keypoint_threshold": 2}, {"minimum_body_points": 13},
    {"minimum_body_points": 3.5}, {"minimum_samples": True},
    {"minimum_samples": 61}, {"horizontal_torso_deg": 90},
    {"upright_torso_deg": 65}, {"max_frame_gap_sec": 3},
    {"found_down_hold_sec": 3}, {"moving_shape_ratio": 1},
])
def test_config_is_validated(kwargs):
    with pytest.raises(ValueError):
        FallCandidateConfig(**kwargs)


def test_config_hash_covers_threshold_changes():
    assert FallCandidateConfig().sha256 != FallCandidateConfig(found_down_hold_sec=1).sha256


def frontal_body():
    """Foreshortened shoulders/hips, visible legs; geometry independent of clip labels."""
    names = [f"{side}_{joint}" for joint in ("shoulder", "hip", "knee", "ankle")
             for side in ("left", "right")]
    coords = ((250, 242), (350, 242), (265, 254), (335, 254),
              (268, 225), (332, 225), (260, 250), (340, 250))
    return PersonPose(.25, (230/640, 218/400, 370/640, 266/400), tuple(
        PoseKeypoint(name, x/640, y/400, .85) for name, (x, y) in zip(names, coords)
    ), 8)


@pytest.mark.parametrize("motion", ["stationary", "moving", "unknown"])
def test_front_facing_lying_body_does_not_require_sideways_torso(motion):
    detector = FallCandidateDetector()
    outputs = [step(detector, {"a": frontal_body()}, n*.2, motion) for n in range(10)]
    candidates = [c for r in outputs for c in r["candidates"]]
    assert len(candidates) == 1
    c = candidates[0]
    assert c["candidateKind"] == "found_down"
    assert c["evidenceEndSec"] == pytest.approx(.6)
    assert "compact_shoulder_hip_leg_layout" in c["reasons"]
    assert not c["evidence"]["pose"]["horizontal"]
    assert c["evidence"]["pose"]["compact_body"]
    assert "lying_surface_unknown" in c["uncertainties"]
    assert "weak_pose_detection" in c["uncertainties"]


@pytest.mark.parametrize("change", [
    "no_legs", "no_hips", "poor_joints", "wide_box_only", "feet_below", "zero_height",
])
def test_compact_candidate_requires_body_layout_not_box_or_hands_alone(change):
    pose = frontal_body()
    if change == "wide_box_only":
        pose = replace(body(), box=(.1, .1, .95, .7))
    elif change == "no_legs":
        pose = replace(pose, keypoints=tuple(p for p in pose.keypoints
                                             if not p.name.endswith(("_knee", "_ankle"))))
    elif change == "no_hips":
        pose = replace(pose, keypoints=tuple(p for p in pose.keypoints
                                             if not p.name.endswith("_hip")))
    elif change == "poor_joints":
        pose = replace(pose, keypoints=tuple(replace(p, confidence=.1) for p in pose.keypoints))
    elif change == "feet_below":
        pose = replace(pose, box=(.3, .4, .75, .85), keypoints=tuple(
            replace(p, y=.85) if p.name.endswith("_ankle") else p for p in pose.keypoints))
    else:
        pose = replace(pose, keypoints=tuple(replace(p, y=.6) for p in pose.keypoints))
    detector = FallCandidateDetector()
    for n in range(10):
        out = step(detector, {"a": pose}, n*.2)
        assert not out["candidates"]
        assert not out["tracks"][0]["features"]["compact_body"]


def test_compact_layout_cannot_override_measured_height_above_floor():
    detector = FallCandidateDetector()
    for n in range(10):
        out = step(detector, {"a": frontal_body()}, n*.2, depth={"a": depth(1)})
        assert not out["candidates"]


def test_compact_layout_does_not_fabricate_a_witnessed_transition():
    detector = FallCandidateDetector()
    step(detector, {"a": body()}, 0)
    out = [step(detector, {"a": frontal_body()}, .2+n*.2) for n in range(5)]
    assert [c["candidateKind"] for r in out for c in r["candidates"]] == ["found_down"]


@pytest.mark.parametrize("kwargs", [
    {"compact_minimum_body_points": 5}, {"compact_minimum_body_points": 6.5},
    {"compact_body_aspect": 1}, {"compact_box_aspect": 1},
    {"compact_max_vertical_fraction": 1.1},
])
def test_compact_config_validation(kwargs):
    with pytest.raises(ValueError):
        FallCandidateConfig(**kwargs)
