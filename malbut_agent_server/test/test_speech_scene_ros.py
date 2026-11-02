"""Demand-driven RGB with warm TF, using real ROS without inference or motion."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread, get_ident
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
    errors = []

    def spin():
        try:
            executor.spin()
        except Exception as error:
            errors.append(error)

    thread = Thread(target=spin)
    thread.start()
    controls['executor_thread'] = thread.ident
    try:
        yield scene, node, publisher, controls
    finally:
        scene.close()
        executor.shutdown(timeout_sec=3)
        thread.join(timeout=3)
        node.destroy_timer(timer)
        node.destroy_node()
        rclpy.shutdown()
        assert not thread.is_alive()
        assert not errors, errors


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
    wait_for(lambda: publisher.get_subscription_count() == 0)
    late_frame = Image(height=1, width=1, step=3, encoding='rgb8', data=[99] * 3)
    late_frame.header.stamp = node.get_clock().now().to_msg()
    scene._image(late_frame)  # A previously dispatched callback cannot retain pixels.
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


def test_repeated_worker_turns_and_close_only_change_readers_on_ros_thread(sensors, monkeypatch):
    scene, node, publisher, controls = sensors
    changed_on = []
    create, destroy = node.create_subscription, node.destroy_subscription

    def create_reader(*args, **kwargs):
        changed_on.append(get_ident())
        return create(*args, **kwargs)

    def destroy_reader(*args, **kwargs):
        changed_on.append(get_ident())
        return destroy(*args, **kwargs)

    monkeypatch.setattr(node, 'create_subscription', create_reader)
    monkeypatch.setattr(node, 'destroy_subscription', destroy_reader)
    with ThreadPoolExecutor(max_workers=1) as pool:
        for pixel in range(1, 21):
            controls['pixel'] = pixel
            context, image = pool.submit(scene.snapshot).result(timeout=3)
            assert context['image']['available'] and image.jpeg == bytes([pixel] * 3)
            assert scene._images._frame is None
        pool.submit(scene.close).result(timeout=1)
    wait_for(lambda: publisher.get_subscription_count() == 0)
    wait_for(lambda: node.count_subscribers('/tf') == 0)
    assert changed_on and set(changed_on) == {controls['executor_thread']}


def test_reader_setup_failure_keeps_dialogue_executor_alive(sensors, monkeypatch):
    scene, node, publisher, _controls = sensors
    create = node.create_subscription

    def fail_image(message_type, *args, **kwargs):
        if message_type is Image:
            raise RuntimeError('camera reader unavailable')
        return create(message_type, *args, **kwargs)

    monkeypatch.setattr(node, 'create_subscription', fail_image)
    with ThreadPoolExecutor(max_workers=1) as pool:
        context, image = pool.submit(scene.snapshot).result(timeout=3)
        assert image is None and not context['image']['available']
        assert publisher.get_subscription_count() == 0
        monkeypatch.setattr(node, 'create_subscription', create)
        context, image = pool.submit(scene.snapshot).result(timeout=3)
        assert context['image']['available'] and image.usable()
    wait_for(lambda: publisher.get_subscription_count() == 0)


def test_callback_from_previous_turn_cannot_supply_next_turn_pixels(sensors):
    scene, node, _publisher, controls = sensors
    controls['publish'] = False
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(scene.snapshot)
        wait_for(lambda: scene._image_subscription is not None)
        old_callback = scene._image_subscription.callback
        assert first.result(timeout=3)[1] is None
        second = pool.submit(scene.snapshot)
        wait_for(lambda: scene._reader_generation == 2)
        late_frame = Image(height=1, width=1, step=3, encoding='rgb8', data=[99] * 3)
        late_frame.header.stamp = node.get_clock().now().to_msg()
        old_callback(late_frame)
        assert scene._images._frame is None
        controls['pixel'] = 42
        controls['publish'] = True
        assert second.result(timeout=3)[1].jpeg == bytes([42] * 3)


def test_busy_executor_does_not_create_a_reader_after_turn_timeout(sensors):
    scene, node, publisher, _controls = sensors
    entered, release, resumed = Event(), Event(), Event()

    def blocked_callback():
        entered.set()
        release.wait(timeout=3)

    blocker = node.create_guard_condition(blocked_callback)
    after_block = node.create_guard_condition(resumed.set)
    blocker.trigger()
    try:
        assert entered.wait(timeout=1)
        with ThreadPoolExecutor(max_workers=1) as pool:
            context, image = pool.submit(scene.snapshot).result(timeout=2)
        assert image is None and not context['image']['available']
    finally:
        release.set()
    after_block.trigger()
    assert resumed.wait(timeout=2)
    assert scene._image_subscription is None
    assert publisher.get_subscription_count() == 0
    with ThreadPoolExecutor(max_workers=1) as pool:
        context, image = pool.submit(scene.snapshot).result(timeout=3)
    assert context['image']['available'] and image.usable()
