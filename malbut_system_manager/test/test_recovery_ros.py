"""Exercise the real launch owner and Action using harmless fixture processes."""

import os
import signal
import subprocess
import sys
import time

from malbut_interfaces.action import ExecuteMission
import rclpy
from rclpy.action import ActionClient
from rclpy.executors import SingleThreadedExecutor


def test_manual_recovery_preserves_live_pid_and_restarts_only_exited_child(tmp_path, monkeypatch):
    """A successful startup, PASS, stopped-child restart and second PASS work end-to-end."""
    fixture = tmp_path / 'child.py'
    fixture.write_text('''import os, pathlib, sys, time
root = pathlib.Path(sys.argv[2])
if sys.argv[1] == 'probe':
    for _ in range(100):
        try:
            for name in ('first', 'second'):
                os.kill(int((root / name).read_text()), 0)
            break
        except (OSError, ValueError):
            time.sleep(0.05)
    else:
        sys.exit(1)
else:
    (root / sys.argv[1]).write_text(str(os.getpid()))
    while True:
        time.sleep(0.1)
''')
    launch_file = tmp_path / 'fixture.launch.py'
    launch_file.write_text(f'''from launch import LaunchDescription
from launch.actions import OpaqueFunction, RegisterEventHandler, SetLaunchConfiguration
from launch.event_handlers import OnProcessExit
from launch_ros.actions import Node
def generate_launch_description():
    def child(name):
        return Node(executable={sys.executable!r},
                    arguments=[{str(fixture)!r}, name, {str(tmp_path)!r}])
    probe = child('probe')
    probe._malbut_readiness_probe = True
    def complete(event, context):
        if event.returncode == 0:
            context.extend_globals({{'malbut_startup_complete': True}})
        return []
    return LaunchDescription([
        SetLaunchConfiguration('malbut_startup_stage', '[0, "fixture"]'),
        RegisterEventHandler(OnProcessExit(target_action=probe, on_exit=complete)),
        child('first'), child('second'), probe])
''')
    # Discovery must use the same scope in both the client and child process.
    # CI has no shell-wide ROS_LOCALHOST_ONLY, unlike the developer shell.
    monkeypatch.setenv('ROS_LOCALHOST_ONLY', '1')
    env = dict(os.environ, ROS_DOMAIN_ID='213')
    context = rclpy.context.Context()
    rclpy.init(context=context, domain_id=213)
    node = rclpy.create_node('recovery_test_client', context=context)
    executor = SingleThreadedExecutor(context=context)
    executor.add_node(node)
    client = ActionClient(node, ExecuteMission, '/malbut/bringup/recover')
    log = tmp_path / 'owner.log'
    with log.open('w') as stream:
        process = subprocess.Popen([
            sys.executable, '-c',
            'from malbut_system_manager.recovery import main; raise SystemExit(main())',
            str(launch_file)], env=env, stdout=stream, stderr=subprocess.STDOUT,
            start_new_session=True)
        try:
            assert client.wait_for_server(timeout_sec=15.0), log.read_text()
            deadline = time.monotonic() + 15.0
            while 'process has finished cleanly' not in log.read_text():
                assert process.poll() is None, log.read_text()
                assert time.monotonic() < deadline, log.read_text()
                time.sleep(0.05)

            def recover():
                future = client.send_goal_async(ExecuteMission.Goal(
                    capability_id='recovery', arguments_yaml='{}'))
                executor.spin_until_future_complete(future, timeout_sec=10.0)
                assert future.done(), log.read_text()
                goal = future.result()
                assert goal.accepted, log.read_text()
                result = goal.get_result_async()
                executor.spin_until_future_complete(result, timeout_sec=15.0)
                assert result.done(), log.read_text()
                assert result.result().status == 4, result.result().result.message
                return result.result().result.result_yaml

            first = int((tmp_path / 'first').read_text())
            second = int((tmp_path / 'second').read_text())
            assert 'restarted: []' in recover()
            assert int((tmp_path / 'first').read_text()) == first
            os.kill(first, signal.SIGTERM)
            deadline = time.monotonic() + 5.0
            while 'process has died' not in log.read_text():
                assert time.monotonic() < deadline, log.read_text()
                time.sleep(0.05)
            assert 'restarted: []' not in recover()
            replacement = int((tmp_path / 'first').read_text())
            assert replacement != first
            assert int((tmp_path / 'second').read_text()) == second
            assert 'restarted: []' in recover()
            assert int((tmp_path / 'first').read_text()) == replacement
        finally:
            os.killpg(process.pid, signal.SIGINT)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            client.destroy()
            executor.shutdown()
            node.destroy_node()
            rclpy.shutdown(context=context)
