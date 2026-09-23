"""Run production pose messages through VLM input; no DDS, weights or Cloud calls."""

import asyncio
import json
from pathlib import Path

import cv2
import numpy as np

from test_detector_pose import camera_image, node  # noqa: F401
from test_fall_candidate import body


def test_detected_person_reaches_cloud_request_once(node, monkeypatch):  # noqa: F811
    """Real tracker/candidate callbacks must satisfy the real consumer schema."""
    monkeypatch.syspath_prepend(str(Path(__file__).parents[3] / 'malbut_agent_server'))
    from malbut_agent_server.application.cloud_fall_monitor import CloudFallMonitor
    from malbut_agent_server.application.fall_detector_input import FallDetectorInput
    from malbut_agent_server.application.fall_frame_buffer import FallFrameBuffer
    from malbut_agent_server.domain.fall_monitoring import (
        CloudFallReply, FallRuntimePolicy, VideoAssessment,
    )

    class Provider:
        execution_target = 'cloud'

        def __init__(self):
            self.calls = []

        async def analyze(self, request):
            self.calls.append(request)
            return CloudFallReply(VideoAssessment.SUSPECTED_FALL, '바닥에 누운 자세')

    provider = Provider()
    policy = FallRuntimePolicy.agreed(
        retry_interval_s=3, max_person_observation_age_s=2, clip_window_s=5,
        max_frame_age_s=2, max_calls_per_minute=5, max_incidents=10, max_images=12)
    monitor = CloudFallMonitor(
        device_id='robot', boot_id='test', policy=policy,
        buffer=FallFrameBuffer(retention_s=10, max_bytes=1000000, max_frames=64),
        provider=provider, clock=lambda: node.now,
    )
    inputs = FallDetectorInput(monitor, max_source_age_s=1)
    inputs.configure(enabled=True, camera_enabled=True, cloud_consent=True, connected=True)
    pixels = np.zeros((400, 640, 3), dtype=np.uint8)
    jpeg = bytes(cv2.imencode('.jpg', pixels)[1])
    node._bridge.imgmsg_to_cv2.return_value = pixels
    node._pose_estimator.estimate_all.return_value = (body('lying'),)
    node._motion_gate.pose_motion_state.return_value = 'stationary'
    # General YOLO sees nobody. Pose still runs and produces its own subject.
    node._model.detect.return_value = {}
    incidents = set()
    for index in range(8):
        node.now = 100 + index * .2
        image = camera_image(node)
        stamp = node._stamp_seconds(image)
        inputs.rgb(jpeg, capture=stamp, frame_id=image.header.frame_id,
                   source_now=node.now, now=node.now)
        node._on_image(image)
        pose_payload = node._poses_publisher.publish.call_args.args[0].data
        candidate_payload = node._fall_candidates_publisher.publish.call_args.args[0].data
        assert json.loads(pose_payload)['status'] == 'ok'
        inputs.poses(pose_payload, source_now=node.now, now=node.now)
        incidents.update(inputs.candidates(
            candidate_payload, source_now=node.now, now=node.now))
    assert len(incidents) == 1
    assert asyncio.run(monitor.run_once())
    assert len(provider.calls) == 1
    request = provider.calls[0]
    assert request.purpose == 'incident'
    assert request.incident_id in incidents
    assert request.subject_key and request.target is not None
    assert 1 <= len(request.window.frames) <= 12
    # The provider is a test double, not a remote endpoint.
