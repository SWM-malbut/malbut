"""Check isolated sensor ownership preserves launch remappings and shutdown."""

import time

import rclpy
from rclpy.context import Context
from rclpy.node import Node
from sensor_msgs.msg import Image

from malbut_patrol.sensor_inputs import SensorInputs


def _wait(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_sensor_node_keeps_resolved_topic_without_duplicate_launch_name():
    context = Context()
    rclpy.init(context=context, args=[
        '--ros-args', '-r', '__node:=named_patrol', '-r', '__ns:=/unit',
        '-r', 'camera:=/remapped_camera',
    ])
    owner = Node('original_patrol', context=context)
    owner.image_input = None
    inputs = SensorInputs(owner, [('image_input', Image, 'camera', lambda _: None)])
    try:
        assert owner.image_input is None
        inputs.set_enabled(True)
        _wait(lambda: owner.image_input is not None)
        assert owner.image_input.topic_name == '/remapped_camera'
        _wait(lambda: ('named_patrol_sensors', '/unit') in
              owner.get_node_names_and_namespaces())
        assert owner.get_node_names().count('named_patrol') == 1
        inputs.set_enabled(False)
        _wait(lambda: owner.image_input is None)
        assert inputs.error is None
    finally:
        inputs.close()
        inputs.close()
        assert context.ok()
        owner.destroy_node()
        context.shutdown()
