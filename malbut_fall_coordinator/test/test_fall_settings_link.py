"""Test native messages and asynchronous callbacks without joining a DDS graph."""

from concurrent.futures import Future
from unittest.mock import Mock

import pytest

from test_fall_settings import make, reply, snapshot, status


class Node:
    def __init__(self):
        self.calls = []
        self.client = Mock()
        self.client.service_is_ready.return_value = True

        def call(request):
            future = Future()
            self.calls.append((request, future))
            return future

        self.client.call_async.side_effect = call

    def create_publisher(self, *args):
        return Mock()

    def create_subscription(self, *args, **kwargs):
        return Mock()

    def create_client(self, *args, **kwargs):
        return self.client

    def create_timer(self, period, callback, **kwargs):
        from rclpy.clock import ClockType
        assert period == 1.0
        assert kwargs['clock'].clock_type == ClockType.STEADY_TIME
        return Mock()


def setup_link():
    pytest.importorskip('rclpy')
    types = pytest.importorskip('malbut_interfaces.msg')
    from malbut_fall_coordinator.fall_settings_link import FallSettingsLink
    node = Node()
    link = FallSettingsLink(node, manager_id='manager', bridge_id='bridge', vlm_id='vlm')
    link.relay, clock = make()
    link.on_status(types.FallRuntimeStatus(**status()))
    link.on_snapshot(types.FallSettingsSnapshot(**snapshot()))
    return node, link, clock


def test_native_service_reply_report_and_no_repeated_application():
    node, link, clock = setup_link()
    from malbut_interfaces.srv import ApplyFallSettings
    from malbut_interfaces.msg import FallSettingsSnapshot
    assert len(node.calls) == 1
    request, future = node.calls[0]
    assert request.runtime_id == 'vlm' and request.settings_revision == 1
    link.tick()
    assert link.heartbeats.publish.call_count == 1
    assert not link.reports.publish.called
    future.set_result(ApplyFallSettings.Response(**reply()))
    message = link.reports.publish.call_args.args[0]
    assert message.applied and message.snapshot_sequence == 2
    clock.value = 101
    link.on_snapshot(FallSettingsSnapshot(**snapshot(
        sequence=3, observed_at=101., server_checked_at=101.)))
    link.tick()
    assert len(node.calls) == 1
    assert link.heartbeats.publish.call_args.args[0].server_checked_at == 101
    link.close()


def test_slow_service_does_not_block_heartbeat_or_forge_report():
    node, link, clock = setup_link()
    old_future = node.calls[0][1]
    for tick in range(101, 104):
        clock.value = tick
        link.tick()
    assert link.heartbeats.publish.call_count == 3
    assert old_future.cancelled()
    assert not link.reports.publish.called
    assert node.client.remove_pending_request.call_args.args[0] is old_future
    clock.value = 104
    link.tick()
    assert len(node.calls) == 2
    link.close()
    assert node.calls[1][1].cancelled()
    assert not link.reports.publish.called
    assert link.timer.cancel.called


def test_transport_exception_does_not_produce_service_failure_reply():
    node, link, clock = setup_link()
    node.calls[0][1].set_exception(RuntimeError('transport lost'))
    assert not link.reports.publish.called
    clock.value = 101
    link.tick()
    assert len(node.calls) == 2
    link.close()
