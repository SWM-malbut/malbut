"""Exercise owned runtime start/stop in mapping mode, without sending movement goals."""

import json
from pathlib import Path
import time
from uuid import uuid4

import rclpy
from geometry_msgs.msg import Twist
from malbut_interfaces.action import DeviceOperation
from rclpy.action import ActionClient
from rclpy.qos import QoSProfile, ReliabilityPolicy
from std_msgs.msg import String

rclpy.init()
node = rclpy.create_node('agent_integration_lifecycle_probe')
client = ActionClient(node, DeviceOperation, '/malbut/device/operate')
states, movement = [], []
request_prefix = 'lifecycle-' + uuid4().hex
node.create_subscription(String, '/malbut/device/state',
                         lambda message: states.append(json.loads(message.data)), 1)
node.create_subscription(
    Twist, '/cmd_vel', lambda value: movement.append([value.linear.x, value.linear.y, value.angular.z])
    if any(abs(v) > .000001 for v in (value.linear.x, value.linear.y, value.angular.z)) else None,
    QoSProfile(depth=10, reliability=ReliabilityPolicy.BEST_EFFORT))
report = {'request_prefix': request_prefix, 'mode': 'mapping', 'movement_goals_sent': 0}


def node_counts():
    names = node.get_node_names_and_namespaces()
    return {name: sum(entry[0] == name for entry in names) for name in (
        'robot_cloud_sync', 'malbut_stt', 'malbut_tts', 'malbut_agent_communication',
        'system_manager', 'homecam_media_agent')}


def wait(predicate, seconds=180):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=.1)
    if not predicate():
        raise RuntimeError('ROS operation timed out')


def operate(operation, arguments):
    sent = client.send_goal_async(DeviceOperation.Goal(
        request_id=request_prefix + '-' + operation, operation=operation,
        arguments_json=json.dumps(arguments)))
    wait(sent.done)
    handle = sent.result()
    if not handle.accepted:
        raise RuntimeError(operation + ' rejected')
    result = handle.get_result_async()
    canceled = False
    deadline = time.monotonic() + 180
    while not result.done() and time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=.1)
        if operation == 'runtime_start' and movement and not canceled:
            handle.cancel_goal_async()
            canceled = True
    if not result.done():
        handle.cancel_goal_async()
        raise RuntimeError(operation + ' result unconfirmed')
    outcome = result.result().result
    return {'success': outcome.success, 'code': outcome.code, 'message': outcome.message,
            'result': json.loads(outcome.result_json)}


try:
    wait(client.server_is_ready, 20)
    wait(lambda: bool(states) and states[-1].get('voice', {}).get('ready'), 90)
    report['before'] = {key: states[-1].get(key) for key in ('runtime', 'voice')}
    report['node_counts_before'] = node_counts()
    report['start'] = operate('runtime_start', {'mode': 'mapping'})
    report['while_running'] = {key: states[-1].get(key) for key in ('runtime', 'voice')}
    report['node_counts_running'] = node_counts()
    report['stop'] = operate('runtime_stop', {'confirmed_mission_ids': []})
    end = time.monotonic() + 4
    while time.monotonic() < end:
        rclpy.spin_once(node, timeout_sec=.1)
    report['after'] = {key: states[-1].get(key) for key in ('runtime', 'voice')}
    report['node_counts_after'] = node_counts()
except Exception as error:
    report['error'] = str(error)
finally:
    report['observed_nonzero_cmd_vel'] = movement
    report['voice_ready_samples'] = [value.get('voice') for value in states]
    Path('/home/ubuntu/agent-integration-ws/lifecycle-probe.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False), flush=True)
    client.destroy()
    node.destroy_node()
    rclpy.shutdown()
