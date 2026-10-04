"""Opt-in sampling characterization, NOT a live-camera or DDS flow test.

Invoke the production VLM image callback and Pose rate gate on a deterministic
10 Hz input schedule. Conversion/inference are omitted, no callback is spun,
and every ROS endpoint is remapped away from production topics.
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from test_cloud_fall_monitor import Clock, Provider


RUN = os.environ.get('MALBUT_RUN_FALL_ROS_TESTS') == '1'
pytestmark = pytest.mark.skipif(not RUN, reason='opt-in production ROS callback check')


@pytest.mark.parametrize('pose_start,expected_common', [(0, 25), (1, 0)])
def test_independent_five_hz_gates_need_not_choose_the_same_images(
        pose_start, expected_common, record_property):
    if os.environ.get('ROS_LOCALHOST_ONLY') != '1':
        raise RuntimeError('diagnostic requires local-only ROS')
    import rclpy
    from homecam_detector.pose import PersonPoseGate
    from malbut_agent_server.fall_runtime import FallNodeSettings
    from malbut_agent_server.ros_fall_monitor import create_fall_node

    settings = FallNodeSettings.parse((Path(__file__).resolve().parents[1]
                                     / 'config/fall_runtime.example.json').read_text())
    prefix = '/diagnosis_' + uuid4().hex
    topics = (
        settings.image_topic, '/homecam/person_poses', '/homecam/fall_candidates',
        '/malbut/falls/settings/apply', '/malbut/falls/control/heartbeat',
        '/malbut/falls/runtime/agent_reply', '/malbut/falls/runtime/subject_observation',
        '/malbut/falls/runtime/decision', '/malbut/falls/runtime/events',
        '/malbut/falls/status',
    )
    args = ['--ros-args', '-r', '__node:=sampling_' + uuid4().hex]
    for topic in topics:
        args.extend(['-r', topic + ':=' + prefix + topic])
    clock, cloud_images, pose_images = Clock(), [], []
    node = None
    rclpy.init(args=args)
    try:
        provider = Provider()
        node = create_fall_node(settings, provider=provider, journal=None, clock=clock)
        # Explicitly isolate rate selection from permission handling, image
        # conversion and detector inference; these are not tested here.
        node.control = SimpleNamespace(accepting_images=True, refresh=lambda: None)
        node.convert_image = cloud_images.append
        pose_gate = PersonPoseGate(5)
        for image_index in range(50):
            # A tiny positive drift avoids binary rounding at the 200ms edge.
            clock.value = 100 + image_index * .100001
            node.on_image(image_index)
            if image_index >= pose_start and pose_gate.should_infer(clock()):
                pose_images.append(image_index)

        common = sorted(set(cloud_images) & set(pose_images))
        assert cloud_images == list(range(0, 50, 2))
        assert pose_images == list(range(pose_start, 50, 2))
        assert len(common) == expected_common
        assert not provider.calls
        result = dict(pose_start_frame=pose_start, input_frames=50,
                      rgb_selected=len(cloud_images), pose_selected=len(pose_images),
                      common_frames=len(common),
                      rgb_first_six=cloud_images[:6], pose_first_six=pose_images[:6])
        record_property('diagnosis', json.dumps(result))
        print(json.dumps(result, sort_keys=True))
    finally:
        if node is not None:
            node.destroy_node()
        rclpy.shutdown()
