"""Connect the real robot to homecam_web using outbound authenticated HTTPS."""

from collections import OrderedDict
from datetime import datetime, timezone
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .web_panel import PanelData, RosBridge, TERMINAL, _terminate, validate_command


TOKEN_PATTERN = re.compile(
    r'hc1\.[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-'
    r'[89ab][0-9a-f]{3}-[0-9a-f]{12}\.[0-9a-f]{64}', re.IGNORECASE)
COMMAND_ID_PATTERN = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-'
    r'[89ab][0-9a-f]{3}-[0-9a-f]{12}', re.IGNORECASE)
ROBOT_INTERFACE = 'malbut_manager_v1'


def validate_backend_url(value):
    """Allow TLS origins and exact loopback HTTP for local development only."""
    if not isinstance(value, str) or any(character.isspace() for character in value):
        raise ValueError('Cloud backend URL is invalid')
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError('Cloud backend URL is invalid') from error
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in ('', '/')
            or '\\' in value or port is not None and not 1 <= port <= 65535
            or not (parsed.scheme == 'https' or parsed.scheme == 'http'
                    and parsed.hostname in ('localhost', '127.0.0.1', '::1'))):
        raise ValueError('Cloud backend requires an HTTPS origin (HTTP is loopback-only)')
    return value.rstrip('/')


def read_device_token(filename):
    """Read only a private regular credential file; never expose its contents."""
    if not filename:
        raise ValueError('HOMECAM_DEVICE_TOKEN_FILE or token_file is required')
    try:
        descriptor = os.open(str(Path(filename).expanduser()), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'r', encoding='ascii') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError('Device token file must be regular and private (0600)')
            token = stream.read(256).strip()
    except (OSError, UnicodeError) as error:
        raise ValueError('Cannot read the protected device token file') from error
    if not TOKEN_PATTERN.fullmatch(token):
        raise ValueError('Device token file has an invalid format')
    return token


class CloudError(RuntimeError):
    """Expose only an HTTP status or a fixed diagnostic, never credentials."""

    def __init__(self, message, status=None):
        """Retain the HTTP status for bounded retry decisions."""
        super().__init__(message)
        self.status = status


class NoRedirect(HTTPRedirectHandler):
    """Never forward a device bearer token to a redirected destination."""

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        """Reject every redirect, including same-origin redirects."""
        return None


class CloudClient:
    """Perform bounded JSON requests with normal certificate verification."""

    def __init__(self, backend_url, token, *, timeout=8.0):
        """Validate configuration without making a network request."""
        self.backend_url = validate_backend_url(backend_url)
        if not TOKEN_PATTERN.fullmatch(token):
            raise ValueError('Invalid device credential')
        self._token = token
        self.timeout = timeout
        self.opener = build_opener(NoRedirect())

    def request(self, path, method='GET', payload=None):
        """Send only fixed device API paths and reject oversized responses."""
        if not path.startswith('/api/device/v1/robot/') or '?' in path or '..' in path:
            raise ValueError('Invalid device API path')
        body = None if payload is None else json.dumps(
            payload, ensure_ascii=False, allow_nan=False,
            separators=(',', ':')).encode('utf-8')
        limit = 2 * 1024 * 1024 if path.endswith('/map') else 64 * 1024
        if body is not None and len(body) > limit:
            raise CloudError('Cloud request exceeds its size limit')
        request = Request(self.backend_url + path, data=body, method=method, headers={
            'Authorization': 'Bearer ' + self._token,
            'Accept': 'application/json', 'Content-Type': 'application/json',
            'User-Agent': 'malbut-real-robot-sync/1',
        })
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(256 * 1024 + 1)
                if len(raw) > 256 * 1024:
                    raise CloudError('Cloud response exceeds its size limit')
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise CloudError('Cloud response must be a JSON object')
                return value
        except HTTPError as error:
            raise CloudError(f'Cloud returned HTTP {error.code}', error.code) from None
        except (URLError, TimeoutError, OSError):
            raise CloudError('Cloud request failed') from None
        except (ValueError, UnicodeError):
            raise CloudError('Cloud response is not valid JSON') from None


def panel_command(operation, payload):
    """Translate only the real-robot allowlist to existing local commands."""
    if not isinstance(payload, dict):
        raise ValueError('Command payload must be an object')
    if operation == 'runtime_start':
        command = {'command': 'bringup_start', **payload}
        if 'command' in payload:
            raise ValueError('Unexpected command field')
    elif operation == 'runtime_stop' and not payload:
        command = {'command': 'bringup_stop'}
    elif operation == 'mission_start' and set(payload) == {'capability', 'arguments'}:
        command = {'command': 'start', **payload}
    elif operation == 'mission_cancel' and not payload:
        command = {'command': 'cancel'}
    else:
        raise ValueError('Operation is not supported by the real robot')
    return validate_command(command)


def bounded_value(value, limit=2048):
    """Bound unstructured ROS feedback without uploading local log files."""
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError):
        return None
    return value if len(raw.encode('utf-8')) <= limit else {'truncated': True}


def state_payload(snapshot, map_info, maps, observed_at=None):
    """Publish the existing cloud schema with an explicit real-robot interface."""
    runtime = {key: snapshot.get('runtime', {}).get(key) for key in (
        'state', 'mode', 'map', 'ready', 'message', 'waiting', 'enabled')}
    runtime['message'] = str(runtime.get('message') or '')[:512]
    runtime['waiting'] = bounded_value(runtime.get('waiting') or [], 2048)
    mode = runtime.get('mode') if runtime.get('state') not in ('STOPPED', 'ERROR') else None
    servers = snapshot.get('servers', {})
    pose = map_info.get('pose') if map_info.get('active') else None
    if mode == 'mapping':
        state = 'waiting_for_map'
    elif mode == 'navigation':
        state = 'ready' if runtime.get('ready') and pose else 'waiting_for_navigation'
    else:
        state = 'idle'
    requests = []
    for item in snapshot.get('requests', [])[-16:]:
        requests.append({key: bounded_value(item[key], 1024) for key in (
            'id', 'capability', 'state', 'route', 'feedback', 'result', 'message')
            if key in item})
        if item.get('capability') == 'autoslam' and item.get('state') not in TERMINAL:
            state = 'exploring'
    result = {
        'state': state, 'message': runtime['message'] or '실로봇 상태를 동기화하고 있습니다.',
        'pose': pose,
        'localization': {'state': 'ok' if pose else 'unavailable', 'tfAgeS': None},
        'nav2': {'robot_interface': ROBOT_INTERFACE, 'runtime_mode': mode or 'stopped',
                 'manager': 'ready' if servers.get('manager') else 'unavailable'},
        'target': {
            'runtime': runtime, 'requests': requests,
            'maps': [{'id': item['id'], 'name': item['name']} for item in maps[:64]],
            'servers': {key: bool(servers.get(key)) for key in ('manager', 'autoslam')},
            'system': bounded_value(snapshot.get('system'), 4096),
            'tracking': bounded_value(snapshot.get('tracking'), 1024),
        },
        'driveMode': {'mode': 'idle', 'state': 'idle', 'sessionId': None, 'message': None},
        'mapRevision': int(map_info.get('version', 0)),
        'observedAt': observed_at or datetime.now(timezone.utc).isoformat(),
    }
    # The server's state body limit is 64 KiB. Retain recent results first and
    # never let an unusually long map catalog stop command polling.
    while len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > 60 * 1024:
        if len(result['target']['requests']) > 1:
            result['target']['requests'].pop(0)
        elif result['target']['maps']:
            result['target']['maps'].pop()
        else:
            raise ValueError('Robot state exceeds its size limit')
    return result


def map_payload(metadata, png, runtime):
    """Upload /map geometry and its matching PNG, not Gazebo-specific artifacts."""
    if (not 1 <= metadata['width'] <= 8192 or not 1 <= metadata['height'] <= 8192
            or not 0.001 <= metadata['resolution'] <= 1.0):
        raise ValueError('Robot map geometry exceeds the cloud contract')
    geometry = {key: value for key, value in metadata.items() if key != 'version'}
    fingerprint = hashlib.sha256(png + json.dumps(
        [geometry, runtime.get('map')], sort_keys=True, default=str).encode()).hexdigest()
    finalized = runtime.get('mode') == 'navigation'
    map_name = str(runtime.get('map') or 'mapping').encode()
    map_id = 'real-' + hashlib.sha256(map_name).hexdigest()[:24]
    origin = metadata['origin']
    return {
        'finalized': finalized,
        'revision': ('real-' if finalized else 'live-') + fingerprint,
        'mapId': map_id, 'mapRevision': fingerprint, 'sourceCreatedAt': None,
        'geometry': {'width': metadata['width'], 'height': metadata['height'],
                     'resolution': metadata['resolution'], 'originX': origin['x'],
                     'originY': origin['y'], 'originYaw': origin['yaw']},
        'previewBase64': base64.b64encode(png).decode('ascii'),
        'userMap': None, 'semanticZones': None,
    }


class CloudSync:
    """Poll off the ROS executor and report dispatch separately from completion."""

    def __init__(self, bridge, client, *, interval=1.0):
        """Prepare an idle bounded worker without starting any robot process."""
        if not 1.0 <= interval <= 60.0:
            raise ValueError('Cloud interval must be between 1 and 60 seconds')
        self.bridge = bridge
        self.client = client
        self.interval = interval
        self.stop_event = threading.Event()
        self.pending = OrderedDict()
        self.seen = OrderedDict()
        self.last_map = ''
        self.last_map_at = 0.0
        self.maps = []
        self.maps_at = 0.0
        self.last_warning = ''
        self.last_warning_at = 0.0
        self.thread = threading.Thread(target=self.run, name='robot-cloud-sync', daemon=True)

    def _complete_pending(self):
        for command_id, result in list(self.pending.items()):
            if self.stop_event.is_set():
                return
            try:
                self.client.request(
                    f'/api/device/v1/robot/commands/{command_id}/complete', 'POST', result)
            except CloudError as error:
                if error.status != 404:
                    raise
                # Already acknowledged or server-side lease expired: never resend the Goal.
            del self.pending[command_id]

    def dispatch(self, command):
        """Acknowledge queue acceptance only; Goal results arrive in state.requests."""
        if not isinstance(command, dict):
            return
        command_id = command.get('id')
        if not isinstance(command_id, str) or not COMMAND_ID_PATTERN.fullmatch(command_id):
            return
        if command_id in self.seen:
            self.pending[command_id] = self.seen[command_id]
            return
        try:
            local = panel_command(command.get('operation'), command.get('payload', {}))
            request_id = self.bridge.submit(local)
            result = {'ok': True, 'result': {
                'accepted': True, 'requestId': request_id,
                'status': 'queued', 'robotInterface': ROBOT_INTERFACE,
            }}
        except (ValueError, RuntimeError) as error:
            result = {'ok': False, 'result': {'error': str(error)[:512]}}
        self.seen[command_id] = result
        self.pending[command_id] = result
        while len(self.seen) > 256:
            self.seen.popitem(last=False)

    def tick(self):
        """Flush receipts before claiming another command; never replay a mission."""
        self._complete_pending()
        if self.stop_event.is_set():
            return
        now = time.monotonic()
        if now - self.maps_at >= 5.0:
            self.maps = self.bridge.catalog.list_maps()
            self.maps_at = now
        snapshot = self.bridge.data.snapshot()
        info = self.bridge.data.map_snapshot()
        self.client.request('/api/device/v1/robot/state', 'POST',
                            state_payload(snapshot, info, self.maps))
        if self.stop_event.is_set():
            return
        if info.get('active') and info.get('available') and now - self.last_map_at >= 5.0:
            try:
                pair = self.bridge.data.map_cache.png()
                if pair is not None:
                    payload = map_payload(*pair, snapshot.get('runtime', {}))
                    if len(payload['previewBase64']) > 1_500_000:
                        raise ValueError('Robot map exceeds the cloud preview size limit')
                    if payload['revision'] != self.last_map:
                        self.client.request('/api/device/v1/robot/map', 'PUT', payload)
                        self.last_map = payload['revision']
            except ValueError:
                self._warn('Robot map cannot be uploaded; command polling remains available')
            finally:
                self.last_map_at = now
        if self.stop_event.is_set():
            return
        response = self.client.request('/api/device/v1/robot/commands')
        commands = response.get('commands')
        if not isinstance(commands, list) or len(commands) > 16:
            raise CloudError('Cloud command response is invalid')
        for command in commands:
            if self.stop_event.is_set():
                break
            self.dispatch(command)
        self._complete_pending()

    def _warn(self, message):
        now = time.monotonic()
        if message != self.last_warning or now - self.last_warning_at >= 30.0:
            self.bridge.node.get_logger().warning(message)
            self.last_warning, self.last_warning_at = message, now

    def run(self):
        """Keep network failures isolated from local ROS and motion execution."""
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self.tick()
            except CloudError as error:
                self._warn(str(error))
            except Exception:
                self._warn('Robot cloud synchronization failed; check local runtime state')
            self.stop_event.wait(max(0.1, self.interval - (time.monotonic() - started)))

    def close(self):
        """Stop claiming commands before the local bridge begins shutdown."""
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=35.0)


def main(args=None):
    """Start an outbound bridge only; an authenticated command starts Bringup."""
    import rclpy
    from rclpy.executors import ExternalShutdownException, SingleThreadedExecutor
    from rclpy.signals import SignalHandlerOptions

    rclpy.init(args=args, signal_handler_options=SignalHandlerOptions.NO)
    previous_term = signal.signal(signal.SIGTERM, _terminate)
    bridge = RosBridge(PanelData(), node_name='robot_cloud_sync', map_topic='/map')
    executor = SingleThreadedExecutor()
    executor.add_node(bridge.node)
    sync = None
    lock = None
    try:
        backend = bridge.node.declare_parameter(
            'backend_url', os.environ.get('HOMECAM_BACKEND_URL', '')).value
        token_file = bridge.node.declare_parameter(
            'token_file', os.environ.get('HOMECAM_DEVICE_TOKEN_FILE', '')).value
        interval = bridge.node.declare_parameter('sync_interval_s', 1.0).value
        token = read_device_token(token_file)
        lock_directory = Path.home() / '.ros/malbut'
        lock_directory.mkdir(parents=True, exist_ok=True)
        lock = (lock_directory / f'cloud-{token.split(".")[1]}.lock').open('a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('A cloud bridge already owns this device credential') from None
        sync = CloudSync(bridge, CloudClient(backend, token), interval=float(interval))
        sync.thread.start()
        bridge.node.get_logger().info('Real robot cloud bridge started; no motion requested')
        executor.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        with bridge.data.lock:
            bridge.data.closed = True
        if sync is not None:
            sync.close()
        if rclpy.ok():
            bridge.cancel_owned()
            if bridge.runtime and bridge.runtime.snapshot()['state'] != 'STOPPED':
                bridge._stop_runtime()
            deadline = time.monotonic() + 31.0
            while ((bridge.stopping_runtime is not None
                    or any(item['state'] not in TERMINAL
                           for item in bridge.data.snapshot()['requests']))
                   and rclpy.ok() and time.monotonic() < deadline):
                executor.spin_once(timeout_sec=0.1)
        if any(item['state'] not in TERMINAL for item in bridge.data.snapshot()['requests']):
            bridge.node.get_logger().warning('Action stop is unconfirmed; check the robot')
        if bridge.runtime:
            try:
                bridge.runtime.close()
            except Exception:
                bridge.node.get_logger().warning('Owned Bringup shutdown failed; check the robot')
        executor.shutdown()
        bridge.node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        if lock is not None:
            lock.close()
        signal.signal(signal.SIGTERM, previous_term)


if __name__ == '__main__':
    main()
