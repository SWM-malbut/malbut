"""Verify launch-owned recovery without sending any robot motion commands."""

import os
from pathlib import Path
import re
import signal
import subprocess
import time

from geometry_msgs.msg import Twist
import rclpy
from rclpy.context import Context
from rclpy.executors import SingleThreadedExecutor
from std_srvs.srv import SetBool


def test_crashed_idle_patrol_respawns_and_serves_processing(tmp_path):
    """An actual child exit must recover the service without replaying a patrol."""
    launch = Path(__file__).resolve().parents[1] / 'launch/patrol.launch.py'
    output = tmp_path / 'launch.log'
    environment = dict(os.environ, ROS_DOMAIN_ID='192', ROS_LOCALHOST_ONLY='1')
    context = Context()
    rclpy.init(context=context, domain_id=192)
    probe = rclpy.create_node('patrol_respawn_probe', context=context)
    executor = SingleThreadedExecutor(context=context)
    executor.add_node(probe)
    commands = []
    probe.create_subscription(Twist, '/cmd_vel', commands.append, 10)
    client = probe.create_client(SetBool, '/patrol_manager/set_processing')

    def pids():
        return [int(pid) for pid in re.findall(
            r'process started with pid \[(\d+)\]', output.read_text())]

    def wait_until(predicate, timeout=20.):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            executor.spin_once(timeout_sec=.05)
            if predicate():
                return
        raise AssertionError(output.read_text())

    def check_service():
        wait_until(client.service_is_ready)
        # Discovery of the request endpoint can precede the response endpoint.
        deadline = time.monotonic() + 2.
        while time.monotonic() < deadline:
            executor.spin_once(timeout_sec=.05)
        future = client.call_async(SetBool.Request(data=False))
        wait_until(future.done)
        assert future.result().success

    with output.open('w') as log:
        process = subprocess.Popen(
            ['ros2', 'launch', str(launch), 'start_enabled:=false',
             'record_observations:=false'], env=environment,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            wait_until(lambda: bool(pids()))
            check_service()
            first = pids()[0]
            stopped_at = time.monotonic()
            os.kill(first, signal.SIGKILL)
            wait_until(lambda: len(pids()) == 2)
            assert pids()[1] != first
            assert time.monotonic() - stopped_at >= 4.5
            check_service()
            assert not commands
            assert 'InvalidHandle' not in output.read_text()
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGINT)
            try:
                process.wait(timeout=15.)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5.)
            executor.shutdown()
            probe.destroy_node()
            rclpy.shutdown(context=context)
