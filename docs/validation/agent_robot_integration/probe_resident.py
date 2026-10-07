"""Read real standby state and send one synthetic status transcript, without movement."""

import hashlib
import json
from pathlib import Path
import time
from uuid import uuid4

import rclpy
from rclpy.action import ActionClient
from malbut_interfaces.action import DeviceOperation
from malbut_interfaces.msg import SpeechPlaybackStatus, SpeechRequest, SpeechTranscript
from std_msgs.msg import String


rclpy.init()
node = rclpy.create_node('agent_integration_standby_probe')
client = ActionClient(node, DeviceOperation, '/malbut/device/operate')
utterance = 'integration-status-' + uuid4().hex
workflow = 'speech-request-' + hashlib.sha256(utterance.encode()).hexdigest()
replies, playback, states = [], [], []
node.create_subscription(String, '/malbut/device/state',
                         lambda message: states.append(json.loads(message.data)), 1)
ids = {utterance, workflow}
node.create_subscription(
    SpeechRequest, '/malbut/speech/response',
    lambda message: replies.append({'text': message.text, 'request_id': message.request_id,
                                    'interim': message.interim, 'playback_id': message.playback_id})
    if message.request_id in ids else None, 10)
node.create_subscription(
    SpeechPlaybackStatus, '/malbut/speech/playback_status',
    lambda message: playback.append({'state': message.state, 'request_id': message.request_id,
                                      'interim': message.interim, 'playback_id': message.playback_id})
    if message.request_id in ids else None, 10)
transcripts = node.create_publisher(SpeechTranscript, '/malbut/speech/transcript', 10)


def wait(predicate, seconds=60):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=0.1)
    if not predicate():
        raise RuntimeError('Expected ROS observation did not arrive')


report = {'utterance_id': utterance, 'workflow_id': workflow,
          'input': 'synthetic SpeechTranscript; no microphone recognition claim'}
try:
    wait(client.server_is_ready)
    wait(lambda: bool(states) and states[-1].get('voice', {}).get('ready'), 90)
    discovery_end = time.monotonic() + 4
    while time.monotonic() < discovery_end:
        rclpy.spin_once(node, timeout_sec=.1)
    sent = client.send_goal_async(DeviceOperation.Goal(
        request_id=utterance + '-status', operation='status', arguments_json='{}'))
    wait(sent.done)
    goal = sent.result()
    if not goal.accepted:
        raise RuntimeError('Status operation rejected')
    result = goal.get_result_async()
    wait(result.done)
    body = result.result().result
    value = json.loads(body.result_json)
    names = node.get_node_names_and_namespaces()
    report.update(
        status_success=body.success, runtime=value.get('runtime'), voice=value.get('voice'),
        map_count=len(value.get('maps', [])), manager_state_present=bool(value.get('system')),
        node_counts={name: sum(entry[0] == name for entry in names) for name in (
            'robot_cloud_sync', 'malbut_stt', 'malbut_tts', 'malbut_agent_communication',
            'system_manager', 'homecam_media_agent')})
    if not body.success or not value.get('voice', {}).get('ready'):
        raise RuntimeError('Resident voice is not ready')
    wait(lambda: transcripts.get_subscription_count() > 0)
    transcripts.publish(SpeechTranscript(utterance_id=utterance, text='로봇 상태 알려 줘'))
    wait(lambda: any(item['request_id'] == workflow and item['state'] in ('finished', 'failed')
                     for item in playback), 90)
    report['workflow_playback_finished'] = any(
        item['request_id'] == workflow and item['state'] == 'finished' for item in playback)
except Exception as error:
    report['error'] = str(error)
finally:
    report.update(replies=replies, playback=playback)
    Path('/home/ubuntu/agent-integration-ws/standby-probe.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False), flush=True)
    client.destroy()
    node.destroy_node()
    rclpy.shutdown()
