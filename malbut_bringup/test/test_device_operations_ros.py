"""Exercise the native device Action without hardware, audio, or external HTTP."""

import threading
import time

import pytest


def test_status_remains_responsive_during_https_and_late_cancel_reports_applied(tmp_path):
    """ROS can serve status while HTTPS waits; a completed save stays a completed save."""
    rclpy = pytest.importorskip('rclpy')
    from malbut_interfaces.action import DeviceOperation
    from rclpy.action import ActionClient
    from rclpy.executors import SingleThreadedExecutor
    from malbut_bringup.device_operations import DeviceOperations, DeviceOperationServer
    from malbut_bringup.web_panel import PanelData, RosBridge
    from malbut_bringup.web_runtime import SavedMapCatalog

    entered, release = threading.Event(), threading.Event()

    class Cloud:
        def request(self, *_args):
            entered.set()
            assert release.wait(5)
            return {'success': True, 'code': 'saved', 'result': {'saved': True},
                    'message': 'saved'}

    rclpy.init()
    bridge = RosBridge(PanelData(), node_name='device_operation_test_bridge')
    bridge.catalog = SavedMapCatalog(tmp_path)
    operations = DeviceOperations(bridge, Cloud(), tmp_path / 'journal.sqlite3')
    server = DeviceOperationServer(bridge, operations)
    caller = rclpy.create_node('device_operation_test_client')
    client = ActionClient(caller, DeviceOperation, '/malbut/device/operate')
    executor = SingleThreadedExecutor()
    for node in (bridge.node, caller):
        executor.add_node(node)

    def wait(predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            executor.spin_once(timeout_sec=0.01)
        assert predicate(), 'ROS operation did not complete'

    try:
        wait(client.server_is_ready)
        sent = client.send_goal_async(DeviceOperation.Goal(
            request_id='settings-1', operation='homecam_settings',
            arguments_json='{"cameraEnabled":false}'))
        wait(sent.done)
        handle = sent.result()
        assert handle.accepted
        result = handle.get_result_async()
        wait(entered.is_set)
        query = client.send_goal_async(DeviceOperation.Goal(
            request_id='status-1', operation='status', arguments_json='{}'))
        wait(query.done)
        query_result = query.result().get_result_async()
        wait(query_result.done)
        assert query_result.result().status == 4
        assert not result.done()
        canceled = handle.cancel_goal_async()
        wait(canceled.done)
        release.set()
        wait(result.done)
        assert result.result().status == 4
        assert result.result().result.code == 'saved'
    finally:
        release.set()
        server.close()
        bridge.runtime.close()
        client.destroy()
        executor.shutdown()
        caller.destroy_node()
        bridge.node.destroy_node()
        rclpy.shutdown()
