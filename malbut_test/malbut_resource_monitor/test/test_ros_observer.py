"""Isolated ROS graph; only a synthetic topic/status publisher, no robot nodes."""

import json
import os
import time

import pytest


@pytest.mark.parametrize('mode', ['light', 'full'])
def test_bulk_topics_are_optional_and_skipped_is_not_zero(tmp_path, monkeypatch, mode):
    rclpy = pytest.importorskip('rclpy')
    pytest.importorskip('malbut_interfaces.msg')
    from nav_msgs.msg import OccupancyGrid
    from rclpy.node import Node
    from sensor_msgs.msg import Image, PointCloud2
    from std_msgs.msg import String
    from malbut_resource_monitor.ros_observer import Observer, channel_name
    from malbut_resource_monitor.store import Store

    monkeypatch.setenv('ROS_DOMAIN_ID', '196')
    monkeypatch.setenv('ROS_LOCALHOST_ONLY', '1')
    rclpy.init()
    store = Store(tmp_path, 1, os.getpid(), buffered=True)
    producer = Node('resource_bulk_test', enable_rosout=False)
    specs = {'/resource_test/rgb': Image, '/resource_test/depth': Image,
             '/resource_test/map': OccupancyGrid, '/resource_test/points': PointCloud2}
    publishers = {name: producer.create_publisher(cls, name, 1) for name, cls in specs.items()}
    small = '/resource_test/small'
    small_pub = producer.create_publisher(String, small, 10)
    observer = Observer(store, [*specs, small, '/resource_test/missing'], topic_mode=mode)
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if (small_pub.get_subscription_count() == 1 and all(
                    name in observer.observation_modes for name in specs) and all(
                    pub.get_subscription_count() == int(mode == 'full')
                    for pub in publishers.values())):
                break
            time.sleep(0.05)
        assert small_pub.get_subscription_count() == 1
        for name, pub in publishers.items():
            assert name in observer.observation_modes
            assert pub.get_subscription_count() == int(mode == 'full')
        for _ in range(5):
            for name, cls in specs.items():
                publishers[name].publish(cls())
            small_pub.publish(String(data='status'))
            time.sleep(0.1)
        observer.sample()
        store.flush()

        def latest(name):
            return json.loads((store.path / (channel_name('topics', name) + '.jsonl'))
                              .read_text().splitlines()[-1])

        for name in specs:
            row = latest(name)
            if mode == 'light':
                assert row['observation_mode'] == 'metadata_only'
                assert not row['subscribed'] and row['publishers'] == 1
                assert all(row[key] is None for key in
                           ('received_hz', 'received_bytes_per_s', 'received_total',
                            'last_message_age_s', 'window_s'))
            else:
                assert row['observation_mode'] == 'received'
                assert row['subscribed'] and row['received_hz'] > 0
        assert latest(small)['received_total'] > 0
        assert latest('/resource_test/missing')['received_total'] is None
        assert observer.error is None
    finally:
        observer.close()
        producer.destroy_node()
        rclpy.shutdown()
        store.close('test')


def test_passive_ros_hz_and_action_transitions(tmp_path, monkeypatch):
    rclpy = pytest.importorskip('rclpy')
    pytest.importorskip('malbut_interfaces.msg')
    from action_msgs.msg import GoalStatus, GoalStatusArray
    from rclpy.node import Node
    from rclpy.qos import DurabilityPolicy, QoSProfile
    from std_msgs.msg import String
    from malbut_interfaces.msg import SpeechPlaybackStatus, SpeechRequest, SpeechTranscript
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
    transcript_pub = producer.create_publisher(SpeechTranscript, '/malbut/speech/transcript', 10)
    response_pub = producer.create_publisher(SpeechRequest, '/malbut/speech/response', 10)
    playback_pub = producer.create_publisher(SpeechPlaybackStatus, '/malbut/speech/playback_status', 10)
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
        for pub in (transcript_pub, response_pub, playback_pub):
            assert pub.get_subscription_count() == 1
        transcript_pub.publish(SpeechTranscript(text='안녕', utterance_id='voice-1'))
        response_pub.publish(SpeechRequest(text='안녕하세요', request_type=0, playback_id='tts-1'))
        playback_pub.publish(SpeechPlaybackStatus(playback_id='tts-1', state='finished'))
        speech_path = store.path / 'speech.jsonl'
        speech = []
        deadline = time.monotonic() + 3
        while len(speech) < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
            if speech_path.exists():
                speech = [json.loads(line) for line in speech_path.read_text().splitlines()
                          if line.endswith('}')]
        by_event = {row['event']: row for row in speech}
        assert by_event['stt_transcript']['text'] == '안녕'
        assert by_event['tts_request']['text'] == '안녕하세요'
        assert by_event['tts_playback']['state'] == 'finished'
        publishers = producer.get_publisher_names_and_types_by_node('malbut_resource_observer', '/')
        assert not {name for name, _ in publishers} - {'/parameter_events'}
        assert observer.error is None
    finally:
        observer.close()
        producer.destroy_node()
        rclpy.shutdown()
        store.close('test')
