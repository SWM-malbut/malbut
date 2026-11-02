"""Demand-driven RGB with warm TF, using real ROS without inference or motion."""

from concurrent.futures import ThreadPoolExecutor
from threading import Thread
import time
from types import SimpleNamespace

import pytest

rclpy = pytest.importorskip('rclpy')
pytest.importorskip('malbut_interfaces.msg')

from geometry_msgs.msg import TransformStamped  # noqa: E402
from rclpy.executors import SingleThreadedExecutor  # noqa: E402
from rclpy.node import Node  # noqa: E402
from rclpy.qos import qos_profile_sensor_data  # noqa: E402
from sensor_msgs.msg import Image  # noqa: E402
from tf2_ros import TransformBroadcaster  # noqa: E402

from malbut_agent_server.speech_scene import RosSpeechScene  # noqa: E402


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    assert predicate()


@pytest.fixture
def sensors(monkeypatch):
    monkeypatch.delenv('HOMECAM_BACKEND_URL', raising=False)
    rclpy.init()
    node = Node('speech_scene_subscription_test')
    publisher = node.create_publisher(Image, '/depth_cam/rgb0/image_raw', qos_profile_sensor_data)
    broadcaster = TransformBroadcaster(node)
    scene = RosSpeechScene(node, SimpleNamespace(map_context=lambda: {'revision': 'test-map'}))
    scene._encode = lambda message: bytes(message.data)
    controls = {'publish': True, 'pixel': 10}

    def publish():
        if not controls['publish']:
            return
        stamp = node.get_clock().now().to_msg()
        image = Image(height=1, width=1, step=3, encoding='rgb8',
                      data=[controls['pixel']] * 3)
        image.header.stamp = stamp
        image.header.frame_id = 'camera'
        publisher.publish(image)
        transform = TransformStamped()
        transform.header.stamp = stamp
        transform.header.frame_id = 'map'
        transform.child_frame_id = 'base_footprint'
        transform.transform.translation.x = 1.25
        transform.transform.rotation.w = 1.
        broadcaster.sendTransform(transform)

    timer = node.create_timer(.04, publish)
    executor = SingleThreadedExecutor()
    executor.add_node(node)
    thread = Thread(target=executor.spin)
    thread.start()
    try:
        yield scene, node, publisher, controls
    finally:
        scene.close()
        executor.shutdown(timeout_sec=3)
        thread.join(timeout=3)
        node.destroy_timer(timer)
        node.destroy_node()
        rclpy.shutdown()


def test_idle_has_no_camera_reader_and_each_turn_gets_fresh_pixels_and_pose(sensors):
    scene, node, publisher, controls = sensors
    assert publisher.get_subscription_count() == 0
    assert node.count_subscribers('/tf') == 1
    with ThreadPoolExecutor() as pool:
        context, image = pool.submit(scene.snapshot).result(timeout=3)
        assert context['image']['available'] and image.usable()
        assert image.jpeg == bytes([10] * 3)
        assert context['pose']['x'] == 1.25
        wait_for(lambda: publisher.get_subscription_count() == 0)
        assert node.count_subscribers('/tf') == 1
        controls['pixel'] = 20
        context, second = pool.submit(scene.snapshot).result(timeout=3)
        assert context['image']['available'] and second.jpeg == bytes([20] * 3)
        assert image.jpeg == bytes([10] * 3)  # A frozen image is never replaced.
    assert scene._images._frame is None
    # Reader destruction is synchronous locally; DDS graph updates may arrive later.
    wait_for(lambda: publisher.get_subscription_count() == 0)
    late_frame = Image(height=1, width=1, step=3, encoding='rgb8', data=[99] * 3)
    late_frame.header.stamp = node.get_clock().now().to_msg()
    scene._image(late_frame)  # A previously dispatched callback cannot retain pixels.
    assert scene._images._frame is None


def test_deferred_turn_waits_until_provider_and_refreshes_map_and_pixels(sensors):
    scene, node, publisher, controls = sensors
    context, pending = scene.prepare_turn()
    assert context['image']['reason'] == 'pending_capture'
    assert publisher.get_subscription_count() == 0
    controls['pixel'] = 42
    scene.missions.map_context = lambda: {'revision': 'new-map'}
    with ThreadPoolExecutor() as pool:
        current, image = pool.submit(pending.resolve).result(timeout=3)
    assert current['map']['revision'] == 'new-map'
    assert current['pose']['x'] == 1.25
    assert image.jpeg == bytes([42] * 3)
    wait_for(lambda: publisher.get_subscription_count() == 0)
    controls['pixel'] = 99
    assert pending.resolve()[1] is image
    assert scene._images._frame is None


def test_guest_image_never_reads_owner_map_or_mission_context(sensors):
    scene, node, publisher, controls = sensors
    scene.missions.map_context = lambda: pytest.fail('guest must not read owner map')
    pending = scene.prepare_image()
    assert publisher.get_subscription_count() == 0
    with ThreadPoolExecutor() as pool:
        context, image = pool.submit(pending.resolve).result(timeout=3)
    assert set(context) == {'image'} and context['image']['available']
    assert image.jpeg == bytes([10] * 3)
    wait_for(lambda: publisher.get_subscription_count() == 0)
    assert scene._images._frame is None


def test_real_encoder_produces_bounded_jpeg_in_memory(sensors):
    scene, node, publisher, controls = sensors
    scene._encode = RosSpeechScene._encode
    with ThreadPoolExecutor() as pool:
        context, image = pool.submit(scene.prepare_image().resolve).result(timeout=3)
    assert context['image']['available'] and image.usable()
    assert image.jpeg.startswith(b'\xff\xd8') and image.jpeg.endswith(b'\xff\xd9')
    wait_for(lambda: publisher.get_subscription_count() == 0)
    assert scene._images._frame is None


def test_missing_sensors_timeout_and_do_not_leave_subscriptions(sensors):
    scene, node, publisher, controls = sensors
    controls['publish'] = False
    scene._buffer.clear()
    started = time.monotonic()
    with ThreadPoolExecutor() as pool:
        context, image = pool.submit(scene.snapshot).result(timeout=3)
    assert time.monotonic() - started < 2
    assert image is None and context['pose'] is None
    assert not context['image']['available']
    wait_for(lambda: publisher.get_subscription_count() == 0)
    assert node.count_subscribers('/tf') == 1


def test_close_interrupts_wait_and_cannot_reenable_sensors(sensors):
    scene, node, publisher, controls = sensors
    controls['publish'] = False
    with ThreadPoolExecutor() as pool:
        future = pool.submit(scene.snapshot)
        wait_for(lambda: publisher.get_subscription_count() > 0)
        started = time.monotonic()
        scene.close()
        assert time.monotonic() - started < .5
        assert future.result(timeout=1)[1] is None
    assert scene.snapshot()[1] is None
    wait_for(lambda: publisher.get_subscription_count() == 0)
    wait_for(lambda: node.count_subscribers('/tf') == 0)


def test_revoked_camera_permission_never_creates_image_reader(sensors):
    scene, node, publisher, _ = sensors
    scene._images.observe_settings(SimpleNamespace(
        server_checked_at=time.monotonic(), camera_enabled=False,
        cloud_consent=True, check_state='confirmed'))
    wait_for(lambda: scene._pose() is not None)
    observed_readers = []
    timer = node.create_timer(.01, lambda: observed_readers.append(publisher.get_subscription_count()))
    try:
        with ThreadPoolExecutor() as pool:
            context, image = pool.submit(scene.snapshot).result(timeout=3)
        assert image is None and not context['image']['available']
        assert context['pose']['x'] == 1.25
        wait_for(lambda: bool(observed_readers))
        assert observed_readers and not any(observed_readers)
    finally:
        node.destroy_timer(timer)


def test_permission_revoked_while_waiting_releases_reader_without_pixels(sensors):
    scene, node, publisher, controls = sensors
    controls['publish'] = False
    with ThreadPoolExecutor() as pool:
        future = pool.submit(scene.snapshot)
        wait_for(lambda: publisher.get_subscription_count() > 0)
        scene._images.observe_settings(SimpleNamespace(
            server_checked_at=time.monotonic(), camera_enabled=False,
            cloud_consent=True, check_state='confirmed'))
        context, image = future.result(timeout=1)
    assert image is None and not context['image']['available']
    assert scene._images._frame is None
    wait_for(lambda: publisher.get_subscription_count() == 0)


def test_absent_camera_does_not_delay_text_only_turn(sensors):
    scene, node, publisher, controls = sensors
    controls['publish'] = False
    node.destroy_publisher(publisher)
    wait_for(lambda: node.count_publishers(scene._rgb_topic) == 0)
    started = time.monotonic()
    context, image = scene.snapshot()
    assert time.monotonic() - started < .5
    assert image is None and not context['image']['available']
