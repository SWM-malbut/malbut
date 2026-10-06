"""ROS node: fetch the owner's OpenAI (대화·목소리) and KMA (날씨) keys from the web (SWM25-235).

Every minute it tells the web which key versions the robot holds, the OpenAI model it uses
and how each key is doing (from /malbut/keys/health and the fall runtime status), and writes
any new key file. Uses the HOMECAM_* settings bringup already passes; without them it stays off.
"""

import argparse
import json
import os
import sys
import threading
import urllib.parse

from malbut_agent_server.service_keys import HEALTH_STATES, key_dir

FALL_STATUS_TOPIC = '/malbut/falls/status'
DEFAULT_OPENAI_MODEL = 'gpt-5.6-luna'
_FALL_CODES = {'cloud_auth_required': 'invalid', 'cloud_quota_exhausted': 'quota'}


class KeyHealthBoard:
    """The latest health per service, from the nodes that use the keys."""

    def __init__(self):
        self._lock = threading.Lock()
        self._latest = {}
        self._fall_seen = None

    def from_message(self, text):
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            return
        if (not isinstance(value, dict) or value.get('service') not in ('openai', 'kma')
                or value.get('state') not in HEALTH_STATES):
            return
        code = value.get('code') if isinstance(value.get('code'), str) else None
        with self._lock:
            self._latest[value['service']] = (value['state'], code)

    def from_fall_status(self, analysis_state, request_id, last_error_code):
        """A finished fall check says whether its Cloud key worked."""
        if not request_id or analysis_state not in ('completed', 'failed'):
            return
        with self._lock:
            if self._fall_seen == (request_id, analysis_state):
                return
            self._fall_seen = (request_id, analysis_state)
            if analysis_state == 'completed':
                self._latest['fall'] = ('ok', None)
            elif last_error_code in _FALL_CODES:
                self._latest['fall'] = (_FALL_CODES[last_error_code], last_error_code)

    def snapshot(self):
        with self._lock:
            return dict(self._latest)


def settings_from_env(environ):
    """(base_url, host, device_id, token_file) or None when the robot is not connected to the web."""
    base_url = (environ.get('HOMECAM_BACKEND_URL') or '').strip()
    token_file = (environ.get('HOMECAM_DEVICE_TOKEN_FILE') or '').strip()
    # The same default device as the homecam launch.
    device_id = (environ.get('HOMECAM_DEVICE_ID') or 'jetson-homecam').strip()
    if not base_url or not token_file or not device_id:
        return None
    host = urllib.parse.urlsplit(base_url).hostname
    return (base_url, host, device_id, token_file) if host else None


def main(argv=None):
    parser = argparse.ArgumentParser(description='Fetch OpenAI/KMA keys the owner set on the web')
    parser.add_argument('--interval-s', type=float, default=60.0)
    args, ros_args = parser.parse_known_args(argv)
    if not 30 <= args.interval_s <= 3600:
        print('key sync interval must be 30-3600 seconds', file=sys.stderr)
        return 2
    settings = settings_from_env(os.environ)
    if settings is None:
        print('key sync off: set HOMECAM_BACKEND_URL and HOMECAM_DEVICE_TOKEN_FILE to use keys from the web')
        return 0
    base_url, host, device_id, token_file = settings
    from malbut_agent_server.adapters.outbound.homecam_service_keys import HomecamServiceKeyClient
    from malbut_agent_server.application.service_key_sync import ServiceKeySync
    from malbut_agent_server.fall_upload_worker import _read_token
    try:
        client = HomecamServiceKeyClient(base_url=base_url, device_id=device_id,
                                         device_token=_read_token(token_file),
                                         allowed_hosts={host}, timeout_s=10)
    except (OSError, ValueError):
        print('key sync off: check HOMECAM_BACKEND_URL and the device token file', file=sys.stderr)
        return 2
    sync = ServiceKeySync(client=client, directory=key_dir(),
                          model=(os.environ.get('OPENAI_MODEL') or DEFAULT_OPENAI_MODEL).strip())
    board = KeyHealthBoard()

    import rclpy
    from rclpy.executors import ExternalShutdownException
    from std_msgs.msg import String
    from malbut_interfaces.msg import FallRuntimeStatus
    from malbut_agent_server.ros_key_health import HEALTH_TOPIC, health_qos

    rclpy.init(args=ros_args)
    node = rclpy.create_node('key_sync')
    node.create_subscription(String, HEALTH_TOPIC, lambda message: board.from_message(message.data), health_qos())
    node.create_subscription(FallRuntimeStatus, FALL_STATUS_TOPIC, lambda status: board.from_fall_status(
        status.analysis_state, status.request_id, status.last_error_code), 10)
    stop = threading.Event()

    def loop():
        # First sync soon after start so a key set while the robot was off arrives quickly.
        delay = 2.0
        while not stop.wait(delay):
            sync.apply(sync.fetch(board.snapshot()))
            delay = args.interval_s

    worker = threading.Thread(target=loop, name='key-sync', daemon=True)
    worker.start()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        stop.set()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
