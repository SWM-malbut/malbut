"""Isolated ROS graph; only a synthetic topic/status publisher, no robot nodes."""

import json
import os
import time

import pytest


def test_passive_ros_hz_and_action_transitions(tmp_path, monkeypatch):
    rclpy = pytest.importorskip('rclpy')
    pytest.importorskip('malbut_interfaces.msg')
    from action_msgs.msg import GoalStatus, GoalStatusArray
    from rclpy.node import Node
    from rclpy.qos import DurabilityPolicy, QoSProfile
    from std_msgs.msg import String
    from malbut_resource_monitor.ros_observer import Observer, channel_name
    from malbut_resource_monitor.store import Store

    monkeypatch.setenv('ROS_DOMAIN_ID', '196')
    monkeypatch.setenv('ROS_LOCALHOST_ONLY', '1')
    rclpy.init()
    store = Store(tmp_path, 1, os.getpid())
    producer = Node('resource_test_producer', enable_rosout=False)
    topic = '/resource_test/sample'
    publisher = producer.create_publisher(String, topic, 10)
    qos = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
    status_pub = producer.create_publisher(GoalStatusArray, '/autoslam/_action/status', qos)
    observer = Observer(store, [topic])
    try:
        deadline = time.monotonic() + 6
        while publisher.get_subscription_count() == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert publisher.get_subscription_count() == 1
        observer.sample()  # Start a fresh measurement window after graph discovery.
        accepted_stamp = producer.get_clock().now().to_msg()
        for index in range(10):
            publisher.publish(String(data='synthetic, not a robot image'))
            if index in (0, 4, 8):
                status = GoalStatus()
                status.goal_info.goal_id.uuid = [42] * 16
                status.goal_info.stamp = accepted_stamp
                status.status = {0: 1, 4: 2, 8: 4}[index]
                status_pub.publish(GoalStatusArray(status_list=[status]))
            time.sleep(0.2)
        observer.sample()
        channel = channel_name('topics', topic)
        records = [json.loads(line) for line in
                   (store.path / (channel + '.jsonl')).read_text().splitlines()]
        assert records[-1]['received_total'] == 10
        assert 4.0 < records[-1]['received_hz'] < 5.5
        action_channel = channel_name('actions', '/autoslam')
        states = [json.loads(line)['state'] for line in
                  (store.path / (action_channel + '.jsonl')).read_text().splitlines()]
        assert states == ['ACCEPTED', 'EXECUTING', 'SUCCEEDED']
        publishers = producer.get_publisher_names_and_types_by_node('malbut_resource_observer', '/')
        assert not {name for name, _ in publishers} - {'/parameter_events'}
        assert observer.error is None
    finally:
        observer.close()
        producer.destroy_node()
        rclpy.shutdown()
        store.close('test')
