"""Exercise real image callbacks without ROS publishers or live camera input."""

import json
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from sensor_msgs.msg import Image
from std_msgs.msg import Bool

import homecam_detector.detector_node as detector_node
from homecam_detector.config import DetectorConfig
from homecam_detector.fall_candidate import FallCandidateDetector
from homecam_detector.fall_pose_control import FallPoseControl
from homecam_detector.pose import PersonPose, PersonPoseGate
from homecam_detector.pose_tracker import PersonPoseTracker


@pytest.fixture
def node(monkeypatch):
    # Bind production callbacks to a test double: no node initialization,
    # subscriptions, DDS publications, event POSTs, or model downloads.
    pose = PersonPose(0.9, (0.1, 0.2, 0.8, 0.9), (), 0)
    value = SimpleNamespace(
        now=1.0,
        _config=DetectorConfig(monitoring_enabled=True),
        _monitoring_state_received=True,
        _bridge=Mock(),
        _model=Mock(),
        _pose_estimator=Mock(),
        _pose_gate=PersonPoseGate(5.0),
        _pose_tracker=PersonPoseTracker(),
        _fall_detector=FallCandidateDetector(),
        _fall_candidates_publisher=Mock(),
        _pose_frame_context=None,
        _last_pose_source_stamp=None,
        _stamp_seconds=detector_node.HomecamDetectorNode._stamp_seconds,
        _pose_failure_count=0,
        _pose_present=False,
        _pose_publisher=Mock(),
        _poses_publisher=Mock(),
        _depth_evidence=Mock(return_value={"usable": False}),
        _motion_gate=Mock(),
        _motion_detector=Mock(),
        _inference_health=Mock(),
        _dedupe=Mock(),
        _segmenter=Mock(),
        _storage_session_id="",
        _poster=None,
        _clip_poster=None,
        get_logger=Mock(return_value=Mock()),
    )
    value._bridge.imgmsg_to_cv2.return_value = np.zeros(
        (8, 8, 3), dtype=np.uint8
    )
    value._model.detect.return_value = {}
    value._pose_estimator.estimate_all.return_value = (pose,)
    value._motion_gate.generic_motion_allowed.return_value = False
    value._motion_gate.pose_motion_state.return_value = "unknown"
    value._dedupe.observe.return_value = []
    value._segmenter.observe.return_value = []
    for name in (
        "_on_image", "_observe_person_pose", "_publish_pose_absent",
        "_publish_tracked_poses",
        "_publish_fall_candidates",
        "_reset_pose_state", "_on_monitoring_state", "_on_parameter_update",
        "_on_fall_status", "_refresh_fall_control",
    ):
        setattr(value, name, MethodType(
            getattr(detector_node.HomecamDetectorNode, name), value
        ))
    monkeypatch.setattr(detector_node, "time", SimpleNamespace(
        monotonic=lambda: value.now, time=lambda: 1000.0 + value.now
    ))
    return value


def test_fall_only_runs_with_recording_off_and_stops_on_status_expiry(node):
    node._config = DetectorConfig(
        fall_only=True, fall_runtime_id='vlm-1', pose_model_path='test.onnx',
        monitoring_enabled=False,
    )
    node._monitoring_state_received = False
    node._fall_control = FallPoseControl('vlm-1', clock=lambda: node.now)
    node._fall_active = False
    node._on_image(camera_image(node))
    node._pose_estimator.estimate_all.assert_not_called()
    state = SimpleNamespace(runtime_id='vlm-1', sequence=1, settings_applied=True,
                            enabled=True, camera_enabled=True, accepting_images=True)
    node._on_fall_status(state)
    node._on_monitoring_state(Bool(data=False))
    node._on_image(camera_image(node))
    node._pose_estimator.estimate_all.assert_called_once()
    node._model.detect.assert_not_called()
    node._dedupe.observe.assert_not_called()
    node._segmenter.observe.assert_not_called()
    assert node._pose_tracker is not None
    node.now += 5
    node._on_image(camera_image(node))
    node._pose_estimator.estimate_all.assert_called_once()
    assert not node._fall_active
    assert json.loads(node._poses_publisher.publish.call_args.args[0].data)['status'] == 'disabled'


def camera_image(node):
    message = Image()
    message.header.frame_id = "test-camera"
    message.header.stamp.sec = int(node.now)
    message.header.stamp.nanosec = round((node.now % 1) * 1e9)
    return message


def payloads(node):
    return [json.loads(call.args[0].data)
            for call in node._pose_publisher.publish.call_args_list]


def multi_payload(node):
    return json.loads(node._poses_publisher.publish.call_args.args[0].data)


@pytest.mark.parametrize("general_result", [{}, {"cat": 0.9}])
def test_pose_runs_without_general_person_result(node, general_result):
    node._model.detect.return_value = general_result

    node._on_image(camera_image(node))

    node._pose_estimator.estimate_all.assert_called_once()
    assert payloads(node)[0]["present"] is True
    # Independent pose output must not invent a person/fall event.
    assert node._dedupe.observe.call_args.args[0] == general_result


def test_pose_runs_without_general_model(node):
    node._model = None
    node._on_image(camera_image(node))
    node._pose_estimator.estimate_all.assert_called_once()
    assert payloads(node)[0]["present"] is True


@pytest.mark.parametrize("error", [RuntimeError, ValueError])
def test_pose_publishes_before_general_inference_failure(node, error):
    def fail(_frame):
        assert payloads(node)[0]["present"] is True
        raise error("general inference failed")

    node._model.detect.side_effect = fail
    node._on_image(camera_image(node))

    node._pose_estimator.estimate_all.assert_called_once()
    node._inference_health.record_failure.assert_called_once()
    assert node._pose_failure_count == 0
    assert node._pose_present


@pytest.mark.parametrize("received,enabled", [
    (False, False), (False, True), (True, False),
])
def test_privacy_guard_blocks_both_models(node, received, enabled):
    node._monitoring_state_received = received
    node._config = DetectorConfig(monitoring_enabled=enabled)
    node._on_image(camera_image(node))
    node._bridge.imgmsg_to_cv2.assert_not_called()
    node._pose_estimator.estimate_all.assert_not_called()
    node._model.detect.assert_not_called()
    assert payloads(node) == []
    node._poses_publisher.publish.assert_not_called()
    node._fall_candidates_publisher.publish.assert_not_called()


def test_parameter_on_cannot_bypass_initial_privacy_state(node):
    node._monitoring_state_received = False
    node._on_parameter_update([
        SimpleNamespace(name="monitoring_enabled", value=True)
    ])
    node._on_image(camera_image(node))
    node._pose_estimator.estimate_all.assert_not_called()
    node._on_monitoring_state(Bool(data=True))
    node._on_image(camera_image(node))
    node._pose_estimator.estimate_all.assert_called_once()


def test_pose_is_rate_limited_and_not_cleared_by_general_absence(node):
    node._on_image(camera_image(node))
    node.now = 1.1
    node._on_image(camera_image(node))
    node.now = 1.19
    node._on_image(camera_image(node))
    node._pose_estimator.estimate_all.assert_called_once()
    assert node._pose_present
    assert len(payloads(node)) == 1
    node.now = 1.2
    node._on_image(camera_image(node))
    assert node._pose_estimator.estimate_all.call_count == 2
    assert len(payloads(node)) == 2


@pytest.mark.parametrize("control", ["topic", "parameter"])
def test_off_clears_pose_and_on_resets_rate_limit(node, control):
    def set_enabled(enabled):
        if control == "topic":
            node._on_monitoring_state(Bool(data=enabled))
        else:
            result = node._on_parameter_update([
                SimpleNamespace(name="monitoring_enabled", value=enabled)
            ])
            assert result.successful

    node._on_image(camera_image(node))
    set_enabled(False)
    assert payloads(node)[-1] == {"present": False}
    assert not node._pose_present
    node.now = 1.1
    node._on_image(camera_image(node))
    node._pose_estimator.estimate_all.assert_called_once()
    set_enabled(True)
    node._on_image(camera_image(node))
    assert node._pose_estimator.estimate_all.call_count == 2
    assert payloads(node)[-1]["present"] is True


def test_only_pose_result_clears_previous_pose(node):
    node._on_image(camera_image(node))
    node._model.detect.return_value = {"person": 0.99}
    node._pose_estimator.estimate_all.return_value = ()
    node.now = 1.2
    node._on_image(camera_image(node))
    assert payloads(node)[-1] == {"present": False}
    assert not node._pose_present


@pytest.mark.parametrize("error", [RuntimeError, ValueError])
def test_pose_failure_does_not_stop_general_detection(node, error):
    node._on_image(camera_image(node))
    node._pose_estimator.estimate_all.side_effect = error("pose failed")
    node._model.detect.return_value = {"person": 0.9, "dog": 0.8}
    node.now = 1.2
    node._on_image(camera_image(node))
    assert node._pose_failure_count == 1
    assert payloads(node)[-1] == {"present": False}
    assert node._dedupe.observe.call_args.args[0] == {
        "person": 0.9, "dog": 0.8
    }


def test_missing_pose_model_does_not_stop_general_detection(node):
    node._pose_estimator = None
    node._on_image(camera_image(node))
    node._model.detect.assert_called_once()
    assert payloads(node) == []


def test_pose_does_not_require_storage_session_or_stationary_robot(node):
    node._config = DetectorConfig(
        monitoring_enabled=True, event_clips_enabled=True
    )
    node._on_image(camera_image(node))
    node._pose_estimator.estimate_all.assert_called_once()
    assert payloads(node)[0]["present"] is True
    node._motion_detector.detect.assert_not_called()
    node._segmenter.discard.assert_called_once()


def test_bad_camera_image_does_not_infer(node):
    node._bridge.imgmsg_to_cv2.side_effect = detector_node.CvBridgeError(
        "invalid camera frame"
    )
    node._on_image(camera_image(node))
    node._pose_estimator.estimate_all.assert_not_called()
    node._model.detect.assert_not_called()
    output = json.loads(node._fall_candidates_publisher.publish.call_args.args[0].data)
    assert output["status"] == "invalid_image"
    assert output["candidates"] == []


def test_multiple_poses_in_one_inference_and_legacy_remains_strong_only(node):
    strong = PersonPose(0.8, (0.1, 0.1, 0.3, 0.8), (), 0)
    weak = PersonPose(0.3, (0.4, 0.5, 0.9, 0.8), (), 0)
    node._pose_estimator.estimate_all.return_value = (strong, weak)
    node._on_image(camera_image(node))
    node._pose_estimator.estimate_all.assert_called_once()
    assert node._pose_estimator.estimate_all.call_args.kwargs == {
        "confidence_threshold": 0.1
    }
    result = multi_payload(node)
    assert result["schemaVersion"] == 1
    assert result["captureStamp"] == {"sec": 1, "nanosec": 0}
    assert result["frameId"] == "test-camera"
    assert len(result["persons"]) == 2
    assert len({p["trackId"] for p in result["persons"]}) == 2
    assert [p["confidenceLevel"] for p in result["persons"]] == ["strong", "weak"]
    assert [p["state"] for p in result["persons"]] == ["tracked", "tentative"]
    assert payloads(node)[-1]["boxConfidence"] == 0.8
    # Depth evidence must be calculated for each person's own geometry.
    assert node._depth_evidence.call_args_list[1].args[0] is weak
    node._pose_estimator.estimate_all.return_value = (weak,)
    node.now += 0.2
    node._on_image(camera_image(node))
    assert payloads(node)[-1] == {"present": False}
    missing, remaining = multi_payload(node)["persons"]
    assert missing["trackId"] == result["persons"][0]["trackId"]
    assert not missing["observed"] and missing["pose"] is None
    assert missing["depthEvidence"] == {"usable": False, "reason": "not_observed"}
    assert remaining["trackId"] == result["persons"][1]["trackId"]


def test_off_clears_all_ids_without_reusing_them(node):
    node._on_image(camera_image(node))
    old_id = multi_payload(node)["persons"][0]["trackId"]
    node._on_monitoring_state(Bool(data=False))
    result = multi_payload(node)
    assert result["status"] == "disabled"
    assert result["persons"] == []
    assert result["expiredTrackIds"] == [old_id]
    node._on_monitoring_state(Bool(data=True))
    assert multi_payload(node)["status"] == "waiting_frame"
    node.now += 0.1
    node._on_image(camera_image(node))
    assert multi_payload(node)["persons"][0]["trackId"] != old_id


def test_duplicate_source_frame_does_not_create_repeated_observations(node):
    message = camera_image(node)
    node._on_image(message)
    node.now += 0.2
    node._on_image(message)
    node._pose_estimator.estimate_all.assert_called_once()
    result = multi_payload(node)
    assert result["status"] == "duplicate_frame"
    assert result["persons"][0]["observationCount"] == 1
    assert result["persons"][0]["pose"] is None
    assert payloads(node)[-1] == {"present": False}


@pytest.mark.parametrize("change", ["clock_reset", "camera_change", "resolution_change"])
def test_camera_discontinuity_starts_new_ids(node, change):
    node._on_image(camera_image(node))
    old = multi_payload(node)["persons"][0]["trackId"]
    node.now += 0.2
    message = camera_image(node)
    if change == "clock_reset":
        message.header.stamp.sec = 0
    elif change == "camera_change":
        message.header.frame_id = "other-camera"
    else:
        node._bridge.imgmsg_to_cv2.return_value = np.zeros((9, 9, 3), dtype=np.uint8)
    node._on_image(message)
    result = multi_payload(node)
    assert result["expiredTrackIds"] == [old]
    assert result["persons"][0]["trackId"] != old
    assert result["persons"][0]["observationCount"] == 1


def test_inference_failure_marks_missing_then_recovers_same_id(node):
    node._on_image(camera_image(node))
    old = multi_payload(node)["persons"][0]["trackId"]
    node._pose_estimator.estimate_all.side_effect = RuntimeError("test")
    node.now += 0.2
    node._on_image(camera_image(node))
    assert multi_payload(node)["status"] == "inference_error"
    assert multi_payload(node)["persons"][0]["pose"] is None
    node._pose_estimator.estimate_all.side_effect = None
    node.now += 0.2
    node._on_image(camera_image(node))
    result = multi_payload(node)
    assert result["persons"][0]["trackId"] == old
    assert result["persons"][0]["observationCount"] == 2


def test_invalid_candidate_is_reported_without_killing_callback(node):
    node._pose_estimator.estimate_all.return_value = (
        PersonPose(0.9, (0.5, 0.2, 0.1, 0.7), (), 0),
    )
    node._on_image(camera_image(node))
    assert multi_payload(node)["status"] == "inference_error"
    node._model.detect.assert_called_once()


def test_fall_candidates_reach_local_output_without_creating_remote_events(node):
    # Four reliable torso joints, with horizontal geometry in the 8x8 test image.
    from homecam_detector.pose import PoseKeypoint
    lying = PersonPose(0.38, (0.1, 0.4, 0.9, 0.6), tuple(
        PoseKeypoint(name, x, y, 0.9) for name, x, y in (
            ("left_shoulder", 0.2, 0.45), ("right_shoulder", 0.2, 0.55),
            ("left_hip", 0.5, 0.45), ("right_hip", 0.5, 0.55),
        )
    ), 4)
    node._pose_estimator.estimate_all.return_value = (lying,)
    for n in range(5):
        node.now = 1+n*0.2
        node._on_image(camera_image(node))
    payloads = [json.loads(call.args[0].data)
                for call in node._fall_candidates_publisher.publish.call_args_list]
    candidates = [candidate for payload in payloads for candidate in payload["candidates"]]
    assert len(candidates) == 1
    assert candidates[0]["candidateKind"] == "found_down"
    assert candidates[0]["targetTrackId"] == multi_payload(node)["persons"][0]["trackId"]
    assert candidates[0]["requiresVerification"]
    assert "weak_pose_detection" in candidates[0]["uncertainties"]
    assert node._pose_estimator.estimate_all.call_count == 5
    assert all(call.args[0] == {} for call in node._dedupe.observe.call_args_list)
    assert node._poster is None and node._clip_poster is None
    node._on_monitoring_state(Bool(data=False))
    disabled = json.loads(node._fall_candidates_publisher.publish.call_args.args[0].data)
    assert disabled["status"] == "disabled"
    assert not disabled["candidates"]
    assert not node._fall_detector._states


def test_fall_analysis_failure_does_not_stop_pose_or_general_detection(node):
    node._fall_detector.update = Mock(side_effect=ValueError("test"))
    node._on_image(camera_image(node))
    payload = json.loads(node._fall_candidates_publisher.publish.call_args.args[0].data)
    assert payload["status"] == "analysis_error"
    assert payload["candidates"] == []
    assert multi_payload(node)["status"] == "ok"
    assert node._pose_present
    node._model.detect.assert_called_once()
