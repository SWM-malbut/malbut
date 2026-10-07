"""Connect the real robot to malbut_web using outbound authenticated HTTPS."""

from collections import OrderedDict
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timezone
import base64
import fcntl
import hashlib
import json
import math
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

import yaml

from .navigation import NavigationError, Navigator
from .drive_mode import FOLLOW_DISTANCE_M, robot_drive_mode
from .web_panel import (
    live_zone_map, PanelData, RosBridge, save_zones, TERMINAL, _terminate, validate_command,
)
from .zones import (
    apply_zone_collection, map_identity, read_zones, with_zone_ids, ZONE_FORMAT, ZoneError,
    zones_path,
)


TOKEN_PATTERN = re.compile(
    r'hc1\.[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-'
    r'[89ab][0-9a-f]{3}-[0-9a-f]{12}\.[0-9a-f]{64}', re.IGNORECASE)
COMMAND_ID_PATTERN = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-'
    r'[89ab][0-9a-f]{3}-[0-9a-f]{12}', re.IGNORECASE)
ROBOT_INTERFACE = 'malbut_manager_v1'
# Answered by this bridge from files and state; no Goal is sent for them.
# rooms_save and zones_apply come from the web map editor, navigation_* from the web
# map screen's destination sending (SWM25-237).
NAVIGATION_OPERATIONS = ('navigation_preview', 'navigation_start', 'navigation_cancel')
LOCAL_OPERATIONS = ('map_delete', 'zones_save', 'rooms_save', 'zones_apply',
                    'robot_ping', 'robot_diagnostics', *NAVIGATION_OPERATIONS)
# A map upload carries the preview PNG, the User Map and the Zones in one body.
MAX_MAP_UPLOAD_BYTES = 2 * 1024 * 1024 - 64 * 1024
# The server accepts device request bodies up to 64 KiB.
MAX_RESULT_BYTES = 60 * 1024
# Held joystick/keyboard input arrives through the command queue. While it does,
# commands are polled every 0.2 s (state uploads keep the normal interval) and
# each velocity is held long enough to ride out one poll plus network jitter.
MANUAL_POLL_S = 0.2
MANUAL_FAST_WINDOW_S = 3.0
MANUAL_HOLD_S = 1.0


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
        # Web map editor commands carry whole rooms and Zones (the server allows 1 MiB).
        response_limit = 2 * 1024 * 1024 if path.endswith('/commands') else 256 * 1024
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(response_limit + 1)
                if len(raw) > response_limit:
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
    elif operation == 'manual_move' and set(payload) == {'vx', 'vy', 'wz'}:
        command = {'command': 'teleop', 'linear_x': payload['vx'], 'linear_y': payload['vy'],
                   'angular_z': payload['wz'], 'hold_s': MANUAL_HOLD_S}
    elif (operation == 'drive_mode_start' and payload.get('mode') == 'patrol'
            and set(payload) <= {'mode', 'thoroughness'}):
        # 지도 탭 › 방 순찰 시작, with the chosen thoroughness (보통 when none is sent).
        command = {'command': 'start', 'capability': 'patrol',
                   'arguments': {'thoroughness': payload.get('thoroughness', 1)}}
    elif (operation == 'drive_mode_start' and payload == {'mode': 'person_following'}):
        # 지도 탭 › 사람 따라가기: the person in front of the 말벗, at a fixed distance.
        command = {'command': 'start', 'capability': 'follow_person', 'arguments': {
            'target_mode': 0, 'target_person_id': '', 'desired_distance_m': FOLLOW_DISTANCE_M}}
    elif (operation == 'drive_mode_stop' and set(payload) == {'mode', 'sessionId'}
            and payload['mode'] in ('patrol', 'person_following')):
        # 중지: the session is the drive's manager mission, wherever it was started.
        command = {'command': 'cancel_mission', 'mission_id': payload['sessionId']}
    elif operation == 'debug_mission_start' and set(payload) == {'capability', 'arguments'}:
        command = {'command': 'debug_start', **payload}
    else:
        raise ValueError('Operation is not supported by the real robot')
    return validate_command(command)


def delete_map(runtime, catalog, map_id):
    """Delete a saved map that no running or starting localization uses."""
    localization = runtime.get('localization') or {}
    in_use = {Path(name).name for name in (runtime.get('map'), localization.get('map')) if name}
    if map_id in in_use:
        raise ValueError('The map in use cannot be deleted; switch maps or stop Bringup first')
    return catalog.delete(map_id)


def cloud_map_id(runtime):
    """Return the web's map ID: one per saved map name (the live map while mapping)."""
    map_name = str(runtime.get('map') or 'mapping').encode()
    return 'real-' + hashlib.sha256(map_name).hexdigest()[:24]


def space_documents(runtime, catalog):
    """
    Return the User Map and Zones of the saved map in use, as the web map editor reads them.

    Returns ``(user_map, zones, map_revision)``. The revision names the map image
    and metadata only, so editing rooms or Zones never makes a map look replaced.
    The Zones carry the developer screen's fields too (map file, editable, message).
    """
    if (runtime.get('localization') or {}).get('mode') != 'LOCALIZATION':
        return None, None, None
    try:
        path = live_zone_map(runtime, catalog)
    except ValueError:
        return None, None, None
    map_id = cloud_map_id(runtime)
    key = (map_id, *(_stamp(item) for item in (
        path, zones_path(path), path.with_suffix('.user-map.geojson'))))
    cached = _SPACE_CACHE.get(str(path))
    if cached is not None and cached[0] == key:
        return cached[1]
    try:
        from .user_map import load_or_build_user_map
        user_map = load_or_build_user_map(path, map_id)
        map_revision = user_map['map_revision']
    except (ValueError, KeyError, OSError):
        # A map the User Map builder cannot read (not trinary) still has Zones.
        user_map, map_revision = None, 'map-' + map_identity(path)[:24]
    try:
        features, message = with_zone_ids(read_zones(path)), ''
    except ZoneError as error:
        features, message = [], f'{error}; applying replaces it'
    zones = {
        'type': 'FeatureCollection', 'format': ZONE_FORMAT, 'map_id': map_id,
        'map_revision': map_revision, 'frame_id': 'map',
        'map': path.name, 'editable': True, 'message': message,
        'features': features,
    }
    result = (user_map, zones, map_revision)
    # Building the User Map reads the whole map image: do it again only when a file changes.
    _SPACE_CACHE.clear()
    _SPACE_CACHE[str(path)] = (key, result)
    return result


_SPACE_CACHE = {}


def _stamp(path):
    try:
        info = Path(path).stat()
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size


def capability_manifests(directory=None):
    """List registered capabilities and their input defaults for remote debugging."""
    if directory is None:
        try:
            from ament_index_python.packages import get_package_share_directory
            directory = Path(get_package_share_directory('malbut_interfaces')) / 'capabilities'
        except (ImportError, KeyError):
            return []
    result = []
    for path in sorted(Path(directory).glob('*.yaml')):
        try:
            document = yaml.safe_load(path.read_text(encoding='utf-8'))
            capability, command = document['capability'], document['command']
            execution = document['execution']
            fields = (document.get('input') or {}).get('fields') or {}
            result.append({
                'id': capability['id'], 'title': capability.get('title', ''),
                'command': command['name'], 'type': command['type'],
                'mode': execution.get('mode'), 'priority': execution.get('priority'),
                'resources': execution.get('resources', []),
                'map_requirement': execution.get('map_requirement'),
                'fields': {name: {'type': spec.get('type'),
                                  **({'default': spec['default']} if 'default' in spec else {})}
                           for name, spec in fields.items()},
            })
        except (OSError, KeyError, TypeError, AttributeError, yaml.YAMLError):
            continue
    return result


def bounded_value(value, limit=2048):
    """Bound unstructured ROS feedback without uploading local log files."""
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError):
        return None
    return value if len(raw.encode('utf-8')) <= limit else {'truncated': True}


def state_payload(snapshot, map_info, maps, observed_at=None, navigation=None):
    """
    Publish the existing cloud schema with an explicit real-robot interface.

    ``navigation`` is the web map screen's destination drive (state, goal, path,
    remaining distance); its fields sit beside the developer screen's in ``target``.
    ``driveMode`` reports room patrol for the map screen's 자율주행 card.
    """
    runtime = {key: snapshot.get('runtime', {}).get(key) for key in (
        'state', 'mode', 'map', 'ready', 'message', 'waiting', 'enabled')}
    runtime['message'] = str(runtime.get('message') or '')[:512]
    runtime['waiting'] = bounded_value(runtime.get('waiting') or [], 2048)
    localization = snapshot.get('runtime', {}).get('localization') or {}
    runtime['localization'] = {
        'mode': localization.get('mode'),
        'map': Path(localization['map']).name if localization.get('map') else None,
        'message': str(localization.get('message') or '')[:512],
    }
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
            'maps': [{'id': item['id'], 'name': item['name'], 'savedAt': _saved_at(item)}
                     for item in maps[:64]],
            'servers': {key: bool(servers.get(key)) for key in ('manager', 'autoslam')},
            'system': bounded_value(snapshot.get('system'), 4096),
            'tracking': bounded_value(snapshot.get('tracking'), 1024),
            'zones': bounded_value(_without_path(snapshot.get('zones')), 1024),
            'manual': bounded_value(snapshot.get('manual'), 512),
            **(navigation or {}),
        },
        'driveMode': robot_drive_mode(
            snapshot.get('system'), snapshot.get('patrol'), _json_object(snapshot.get('tracking')),
            ready=bool(mode == 'navigation' and runtime.get('ready') and pose
                       and servers.get('manager')),
            can_follow=bool(servers.get('follow_person'))),
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


def _json_object(value):
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _saved_at(item):
    """When a saved map was last written (made or remade), for the map tab's list."""
    try:
        stamp = Path(item['path']).stat().st_mtime
    except (KeyError, OSError, TypeError):
        return None
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def _without_path(state):
    """Report a Zone state's map by filename, not its local path."""
    if not isinstance(state, dict) or not state.get('map'):
        return state
    return {**state, 'map': Path(str(state['map'])).name}


def map_payload(metadata, png, runtime, zones=None, user_map=None, map_revision=None):
    """
    Preserve /map geometry and its native-size, neutral occupancy PNG.

    ``revision`` changes with any upload content (so edits upload again);
    ``mapRevision`` names only the map itself (the web map editor's drafts keep it).
    """
    if (not 1 <= metadata['width'] <= 8192 or not 1 <= metadata['height'] <= 8192
            or not 0.001 <= metadata['resolution'] <= 1.0):
        raise ValueError('Robot map geometry exceeds the cloud contract')
    geometry = {key: value for key, value in metadata.items() if key != 'version'}
    base = hashlib.sha256(png + json.dumps(
        [geometry, runtime.get('map')], sort_keys=True, default=str).encode()).hexdigest()
    # Room and Zone edits are uploaded with the saved map they belong to.
    fingerprint = hashlib.sha256((base + json.dumps(
        [zones, user_map], sort_keys=True, default=str)).encode()).hexdigest()
    finalized = runtime.get('mode') == 'navigation'
    origin = metadata['origin']
    payload = {
        'finalized': finalized,
        'revision': ('real-' if finalized else 'live-') + fingerprint,
        'mapId': cloud_map_id(runtime), 'mapRevision': map_revision or base,
        'sourceCreatedAt': None,
        'geometry': {'width': metadata['width'], 'height': metadata['height'],
                     'resolution': metadata['resolution'], 'originX': origin['x'],
                     'originY': origin['y'], 'originYaw': origin['yaw']},
        'previewBase64': base64.b64encode(png).decode('ascii'),
        'userMap': user_map, 'semanticZones': zones,
    }
    # The wall outline only decorates the map; drop it, then the User Map, before
    # the upload outgrows the server's limit (the map and Zones still upload).
    for trim in ('wall_outline', 'user_map'):
        if len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) <= MAX_MAP_UPLOAD_BYTES:
            break
        if payload['userMap'] is None:
            break
        if trim == 'wall_outline':
            payload['userMap'] = {**payload['userMap'], 'features': [
                feature for feature in payload['userMap']['features']
                if (feature.get('properties') or {}).get('role') != 'wall_outline']}
        else:
            payload['userMap'] = None
    return payload


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
        self.last_state_at = -math.inf
        self.fast_until = -math.inf
        self.maps = []
        self.maps_at = 0.0
        self.last_warning = ''
        self.last_warning_at = 0.0
        self.capabilities = None
        self.navigator = Navigator()
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
        """Acknowledge queued Goals; map, Zone and query requests answer directly."""
        if not isinstance(command, dict):
            return
        command_id = command.get('id')
        if not isinstance(command_id, str) or not COMMAND_ID_PATTERN.fullmatch(command_id):
            return
        if command_id in self.seen:
            self.pending[command_id] = self.seen[command_id]
            return
        operation, payload = command.get('operation'), command.get('payload', {})
        try:
            if operation in LOCAL_OPERATIONS:
                result = {'ok': True, 'result': self._local(operation, payload)}
            else:
                request_id = self.bridge.submit(panel_command(operation, payload))
                result = {'ok': True, 'result': {
                    'accepted': True, 'requestId': request_id,
                    'status': 'queued', 'robotInterface': ROBOT_INTERFACE,
                }}
        except (ValueError, RuntimeError, OSError, FutureTimeout) as error:
            message = str(error)[:512] or 'Robot query timed out'
            result = {'ok': False, 'result': {'error': message}}
        if len(json.dumps(result, ensure_ascii=False, default=str).encode('utf-8')) > (
                MAX_RESULT_BYTES):
            result = {'ok': False, 'result': {'error': 'Result exceeds the cloud size limit'}}
        self.seen[command_id] = result
        self.pending[command_id] = result
        if operation == 'manual_move' and result['ok']:
            self.fast_until = time.monotonic() + MANUAL_FAST_WINDOW_S
        while len(self.seen) > 256:
            self.seen.popitem(last=False)

    def _local(self, operation, payload):
        if not isinstance(payload, dict):
            raise ValueError('Command payload must be an object')
        runtime = self.bridge.data.snapshot()['runtime']
        if operation == 'map_delete':
            if set(payload) != {'map'}:
                raise ValueError('Map deletion needs only the map ID')
            removed = delete_map(runtime, self.bridge.catalog, payload['map'])
            self.maps_at = 0.0  # List the catalog again on the next tick.
            return {'deleted': payload['map'], 'files': removed}
        if operation == 'zones_save':
            count = save_zones(runtime, self.bridge.catalog, payload)
            self.last_map_at = 0.0  # Upload the saved map with its new Zones.
            return {'saved': count, 'map': payload['map']}
        if operation in NAVIGATION_OPERATIONS:
            return self._navigation(operation, payload, runtime)
        if operation in ('rooms_save', 'zones_apply'):
            path = live_zone_map(runtime, self.bridge.catalog)
            map_id = cloud_map_id(runtime)
            _, _, map_revision = space_documents(runtime, self.bridge.catalog)
            if operation == 'rooms_save':
                from .user_map import save_rooms
                count = len(save_rooms(path, map_id, payload)['features'])
                result = {'saved': len(payload['rooms']), 'features': count}
            else:
                # The zone filter reloads its mask when this file changes.
                result = {'saved': apply_zone_collection(path, payload, map_id, map_revision),
                          'nav2_reloaded': True}
            self.last_map_at = 0.0  # Upload the map with its new rooms or Zones.
            return {**result, 'map_id': map_id, 'map_revision': map_revision}
        if payload:
            raise ValueError('This request takes no payload')
        now = datetime.now(timezone.utc).isoformat()
        if operation == 'robot_ping':
            return {'pong': True, 'robotTime': now}
        if self.capabilities is None:
            self.capabilities = capability_manifests()
        diagnostics = self.bridge.call(self.bridge.diagnostics)
        diagnostics.update(
            robotTime=now, capabilities=self.capabilities,
            maps=[{'id': item['id'], 'name': item['name']} for item in self.maps[:64]],
            cloud={'interval_s': self.interval, 'pending_receipts': len(self.pending),
                   'last_warning': self.last_warning})
        return json.loads(json.dumps(diagnostics, default=str))

    def _navigation(self, operation, payload, runtime):
        """Preview, start or cancel a destination drive picked on the web map."""
        requests = self.bridge.data.snapshot().get('requests', [])
        busy = self.navigator.busy(requests)
        if operation == 'navigation_cancel':
            if set(payload) != {'sessionId'} or not isinstance(payload['sessionId'], str):
                raise ValueError('Navigation cancel needs only the session ID')
            return self.navigator.cancel(payload['sessionId'], lambda request_id: (
                self.bridge.submit({'command': 'cancel', 'request_id': request_id})))
        user_map, zones, map_revision = space_documents(runtime, self.bridge.catalog)
        if runtime.get('mode') != 'navigation' or zones is None:
            raise NavigationError('말벗이 저장된 지도로 주행 중일 때 보낼 수 있어요.')
        map_key = f'{cloud_map_id(runtime)}:{map_revision}'
        if operation == 'navigation_start':
            if set(payload) != {'previewToken'} or not isinstance(payload['previewToken'], str):
                raise ValueError('Navigation start needs only the preview token')
            return self.navigator.start(
                payload['previewToken'], map_key=map_key, busy=busy,
                submit=lambda goal: self.bridge.submit({
                    'command': 'start', 'capability': 'navigate_to_pose',
                    'arguments': {key: goal[key] for key in ('x', 'y', 'yaw')}}))
        if (set(payload) != {'x', 'y'} or not all(
                type(payload[key]) in (int, float) and math.isfinite(payload[key])
                for key in ('x', 'y'))):
            raise ValueError('Navigation preview needs finite x and y')
        if user_map is None:
            raise NavigationError('이 지도에는 다닐 수 있는 바닥 정보가 없어 보낼 수 없어요.')
        info = self.bridge.data.map_snapshot()
        try:
            return self.navigator.preview(
                float(payload['x']), float(payload['y']),
                pose=info.get('pose') if info.get('active') else None, map_key=map_key,
                floor=[feature['geometry'] for feature in user_map['features']
                       if (feature.get('properties') or {}).get('role') == 'walkable_area'],
                blocked=[feature['geometry'] for feature in zones['features']
                         if feature['properties'].get('behavior') == 'restricted'],
                plan=self.bridge.plan_path, busy=busy)
        except NavigationError as error:
            # The owner reads a plain reason; the robot log keeps the planner's own.
            if error.__cause__ is not None:
                self._warn(f'Destination preview: {error.__cause__}')
            raise

    def wait_seconds(self, elapsed):
        """Poll fast only while manual input keeps arriving."""
        interval = MANUAL_POLL_S if time.monotonic() < self.fast_until else self.interval
        return max(0.1, interval - elapsed)

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
        # Fast manual polls only claim commands; state keeps its normal cadence.
        if now - self.last_state_at >= self.interval - MANUAL_POLL_S / 2:
            navigation = self.navigator.target(snapshot.get('requests', []))
            self.client.request('/api/device/v1/robot/state', 'POST',
                                state_payload(snapshot, info, self.maps, navigation=navigation))
            self.last_state_at = now
        if self.stop_event.is_set():
            return
        if info.get('active') and info.get('available') and now - self.last_map_at >= 5.0:
            try:
                pair = self.bridge.data.map_cache.png()
                if pair is not None:
                    runtime = snapshot.get('runtime', {})
                    user_map, zones, map_revision = space_documents(runtime, self.bridge.catalog)
                    payload = map_payload(*pair, runtime, zones, user_map, map_revision)
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
            self.stop_event.wait(self.wait_seconds(time.monotonic() - started))

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
    bridge = RosBridge(PanelData(map_palette='map'),
                       node_name='robot_cloud_sync', map_topic='/map')
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
            bridge.stop_teleop()
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
