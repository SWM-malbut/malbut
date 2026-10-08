"""Fixed, journaled device operations owned by the resident cloud bridge."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
import time

from .web_panel import OwnerCallTimeout, save_zones, zone_view


CLOUD_OPERATIONS = frozenset({
    'homecam_status', 'homecam_events', 'homecam_recordings', 'homecam_falls',
    'homecam_settings', 'result_publish',
})
OPERATIONS = CLOUD_OPERATIONS | {
    'status', 'runtime_start', 'runtime_stop', 'map_list', 'map_select', 'map_delete',
    'zones_get', 'zones_update',
}
MISSION_COLLECTIONS = ('active_foreground_missions', 'active_background_missions',
                       'suspended_missions', 'pending_missions')


class OperationError(RuntimeError):
    """Return a fixed failure code and bounded diagnostic to the caller."""

    def __init__(self, code, message, result=None):
        super().__init__(message)
        self.code = code
        self.result = result or {}


def active_missions(snapshot):
    """Read Manager-owned missions, independent of this bridge's own requests."""
    system = snapshot.get('system') or {}
    return {item['mission_id']: item for key in MISSION_COLLECTIONS
            for item in system.get(key, []) if item.get('mission_id')}


def validate_operation(operation, arguments):
    """Reject arbitrary paths, commands, polygons and unsupported operations."""
    if operation not in OPERATIONS or not isinstance(arguments, dict):
        raise ValueError('Unsupported device operation')
    if len(json.dumps(arguments, allow_nan=False).encode()) > 16384:
        raise ValueError('Device operation arguments exceed 16 KiB')
    if operation in CLOUD_OPERATIONS:
        return  # The authenticated backend validates its own exact field allowlist.
    fields = {
        'status': set(), 'map_list': set(), 'zones_get': set(),
        'runtime_start': {'mode', 'map', 'last_map', 'movement_runtime_id', 'movement_epoch'},
        'runtime_stop': {'confirmed_mission_ids'},
        'map_select': {'map', 'movement_runtime_id', 'movement_epoch'},
        'map_delete': {'map', 'confirmed', 'revision'},
        'zones_update': {'map', 'index', 'revision', 'name', 'behavior'},
    }[operation]
    if set(arguments) - fields:
        raise ValueError('Unexpected operation fields')
    if operation in ('runtime_start', 'map_select') and (
            'movement_runtime_id' in arguments or 'movement_epoch' in arguments):
        if (not isinstance(arguments.get('movement_runtime_id'), str)
                or not arguments['movement_runtime_id']
                or type(arguments.get('movement_epoch')) is not int
                or arguments['movement_epoch'] < 0):
            raise ValueError('Invalid preparation movement binding')
    if operation == 'runtime_start':
        if arguments.get('mode') not in ('mapping', 'navigation'):
            raise ValueError('Select mapping or navigation')
        if arguments['mode'] == 'mapping' and 'map' in arguments:
            raise ValueError('Mapping does not load a saved map')
        if 'last_map' in arguments and (arguments['mode'] != 'mapping'
                                        or arguments['last_map'] is not False):
            raise ValueError('Only a mapping start can skip the last chosen map')
    if (operation in ('map_select', 'map_delete', 'zones_update')
            or operation == 'runtime_start' and arguments['mode'] == 'navigation'):
        name = arguments.get('map')
        if (not isinstance(name, str) or Path(name).name != name
                or Path(name).suffix not in ('.yaml', '.yml')):
            raise ValueError('Choose an exact map ID from the catalog')
    if operation == 'map_delete' and arguments.get('confirmed') is not True:
        raise OperationError('confirmation_required', 'Confirm the exact map before deletion')
    if operation == 'map_delete' and not isinstance(arguments.get('revision'), str):
        raise ValueError('Map deletion requires the confirmed catalog revision')
    if operation == 'runtime_stop':
        ids = arguments.get('confirmed_mission_ids', [])
        if not isinstance(ids, list) or len(ids) > 64 or any(
                not isinstance(item, str) or len(item) > 128 for item in ids):
            raise ValueError('Invalid confirmed mission IDs')
    if operation == 'zones_update':
        if (type(arguments.get('index')) is not int or arguments['index'] < 0
                or not isinstance(arguments.get('revision'), str)
                or not ({'name', 'behavior'} & set(arguments))):
            raise ValueError('Select an existing Zone index and its revision')
        if 'name' in arguments and (not isinstance(arguments['name'], str)
                                    or len(arguments['name']) > 64):
            raise ValueError('Zone name must be at most 64 characters')
        if ('behavior' in arguments
                and arguments['behavior'] not in ('allow', 'avoid', 'restricted')):
            raise ValueError('Invalid Zone behavior')


class DeviceOperations:
    """Run bounded preparation off ROS and never replay an ambiguous mutation."""

    def __init__(self, bridge, cloud, path=None):
        self.bridge, self.cloud = bridge, cloud
        path = Path(path or '~/.local/state/malbut/device-operations.sqlite3').expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS operations ('
                        'request_id TEXT PRIMARY KEY, operation TEXT, arguments TEXT,'
                        'state TEXT, response TEXT)')
        self.db.execute("UPDATE operations SET state='unknown' WHERE state='sent'")
        self.db.commit()
        self.db_lock = threading.RLock()
        self.mutation_lock = threading.Lock()

    def close(self):
        """Close the journal after all operation workers finish."""
        with self.db_lock:
            self.db.close()

    def execute(self, request_id, operation, arguments, canceled=None):
        """Persist admission before sending and retain an idempotent terminal result."""
        canceled = canceled or threading.Event()
        admitted = False
        try:
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', request_id):
                raise ValueError('Invalid operation request ID')
            validate_operation(operation, arguments)
            encoded = json.dumps(arguments, sort_keys=True, allow_nan=False)
            with self.db_lock:
                prior = self.db.execute(
                    'SELECT operation, arguments, state, response FROM operations '
                    'WHERE request_id=?',
                    (request_id,)).fetchone()
                if prior:
                    if prior[:2] != (operation, encoded):
                        raise OperationError(
                            'request_conflict', 'Request ID belongs to another operation')
                    if prior[3]:
                        if operation not in CLOUD_OPERATIONS:
                            return json.loads(prior[3])
                        # Device-scoped backend idempotency rechecks current
                        # delegation; a cached sensitive reply is not authority.
                    else:
                        raise OperationError(
                            'result_unknown', 'Prior dispatch is unconfirmed; it was not retried')
                else:
                    self.db.execute('INSERT INTO operations VALUES (?,?,?,?,NULL)',
                                    (request_id, operation, encoded, 'sent'))
                    self.db.commit()
                admitted = True
            self._check_cancel(canceled)
            if operation in (
                    'runtime_start', 'runtime_stop', 'map_select', 'map_delete', 'zones_update'):
                with self.mutation_lock:
                    self._check_cancel(canceled)
                    result = self._local(operation, arguments, canceled)
            elif operation in CLOUD_OPERATIONS:
                result = self._cloud(request_id, operation, arguments)
                return self._finish(request_id, result)
            else:
                result = self._local(operation, arguments, canceled)
            return self._finish(request_id, {
                'success': True, 'code': 'completed', 'result': result,
                'message': 'Operation completed'})
        except Exception as error:
            result = {'success': False, 'code': getattr(error, 'code', 'operation_failed'),
                      'result': getattr(error, 'result', {}), 'message': str(error)[:512]}
            # Conflicting IDs and unknown prior sends must not overwrite their journal.
            if result['code'] in ('request_conflict', 'result_unknown'):
                return result
            return self._finish(request_id, result) if admitted else result

    def _cloud(self, request_id, operation, arguments):
        result = self.cloud.request('/api/device/v1/agent/operate', 'POST', {
            'requestId': request_id, 'operation': operation, 'arguments': arguments})
        if not isinstance(result.get('success'), bool):
            raise OperationError('invalid_response', 'Invalid device API result')
        return result

    def _finish(self, request_id, response):
        encoded = json.dumps(response, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode()) > 60000:
            response = {'success': False, 'code': 'result_too_large', 'result': {},
                        'message': 'Operation result exceeds 60 KiB'}
            encoded = json.dumps(response)
        with self.db_lock:
            self.db.execute('UPDATE operations SET state=?, response=? WHERE request_id=?',
                            ('terminal', encoded, request_id))
            self.db.commit()
        return response

    @staticmethod
    def _check_cancel(canceled):
        if canceled.is_set():
            raise OperationError('canceled', 'Operation canceled before the next effect')

    def status(self):
        """Distinguish latched mode from live sensor evidence and unknown battery."""
        snapshot = self.bridge.data.snapshot()
        runtime = snapshot['runtime']
        runtime.pop('log_path', None)
        runtime.pop('log_tail', None)
        localization = runtime.get('localization') or {}
        if localization.get('map'):
            localization['map'] = Path(localization['map']).name
        now = time.time()
        person = snapshot.get('observations', {}).get('person')
        if person:
            person['current'] = (runtime.get('state') == 'RUNNING'
                                 and 0 <= now - person['observed_at'] <= 2.0)
        observed = snapshot.get('tracking_observed_at')
        snapshot['tracking'] = {'value': snapshot.get('tracking'), 'observed_at': observed,
                                'current': observed is not None and 0 <= now - observed <= 2.0}
        snapshot['maps'] = [{k: item[k] for k in ('id', 'name', 'revision') if k in item}
                            for item in self.bridge.catalog.list_maps()]
        snapshot['last_selected_map'] = (self.bridge.runtime.last_selected_map()
                                         if self.bridge.runtime else None)
        snapshot['map_id'] = ('real-' + hashlib.sha256(runtime['map'].encode()).hexdigest()[:24]
                              if runtime.get('mode') == 'navigation' and runtime.get('map')
                              else None)
        from .cloud_sync import bounded_value, capability_manifests
        snapshot['capabilities'] = capability_manifests()
        snapshot['requests'] = [
            {key: bounded_value(value, 2048) for key, value in item.items()}
            for item in snapshot['requests'][-16:]]
        snapshot['recent_results'] = snapshot.get('recent_results', [])[-10:]
        while len(json.dumps(snapshot, ensure_ascii=False).encode()) > 54000:
            if snapshot['recent_results']:
                snapshot['recent_results'].pop(0)
            elif snapshot['requests']:
                snapshot['requests'].pop(0)
            else:
                break
        snapshot['battery'] = None
        snapshot['observed_at'] = now
        return snapshot

    def _wait(self, predicate, canceled, timeout=180.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_cancel(canceled)
            result = predicate()
            if result:
                return result
            canceled.wait(0.1)
        raise OperationError('result_unknown', 'Operation completion could not be confirmed')

    def _wait_ros(self, future, canceled):
        self._wait(future.done, canceled)
        return future.result()

    def _local(self, operation, arguments, canceled):
        bridge = self.bridge
        if operation == 'status':
            return self.status()
        if operation == 'map_list':
            state = self.status()
            return {key: state[key] for key in ('maps', 'last_selected_map')}
        if operation == 'zones_get':
            return self._zones()
        if operation == 'map_delete':
            from .cloud_sync import delete_map

            def remove():
                target = next((item for item in bridge.catalog.list_maps()
                               if item['id'] == arguments['map']), None)
                if target is None or target.get('revision') != arguments['revision']:
                    raise OperationError(
                        'target_changed', 'The confirmed map changed; query it again')
                self._check_cancel(canceled)
                return delete_map(
                    bridge.data.snapshot()['runtime'], bridge.catalog, arguments['map'])
            removed = bridge.call(remove)
            return {'deleted': arguments['map'], 'files': removed}
        if operation == 'zones_update':
            def update():
                view = self._zones()
                index = arguments['index']
                if (arguments['map'] != view['map'] or arguments['revision'] != view['revision']
                        or index >= len(view['zones'])):
                    raise OperationError(
                        'target_changed', 'The map or Zone changed; query it again')
                for field in ('name', 'behavior'):
                    if field in arguments:
                        view['zones'][index][field] = arguments[field]
                self._check_cancel(canceled)
                save_zones(bridge.data.snapshot()['runtime'], bridge.catalog,
                           {'map': view['map'], 'zones': view['zones']})
                return self._zones()
            return bridge.call(update)
        if operation == 'runtime_stop':
            def stop():
                ids = set(active_missions(bridge.data.snapshot()))
                if ids - set(arguments.get('confirmed_mission_ids', [])):
                    raise OperationError(
                        'preemption_confirmation_required',
                        'Confirm the active mission IDs before stopping robot functions',
                        {'conflicting_mission_ids': sorted(ids)})
                self._check_cancel(canceled)
                if bridge.runtime and bridge.runtime.snapshot()['state'] != 'STOPPED':
                    bridge._stop_runtime(
                        confirmed_mission_ids=arguments.get('confirmed_mission_ids', []))
            bridge.call(stop)

            def stopped():
                state = bridge.data.snapshot()['runtime']
                if state['state'] == 'STOPPED':
                    return True
                error = bridge.runtime_stop_error
                if bridge.stopping_runtime is None and error:
                    raise OperationError(error['code'], error['message'], error['result'])
                if bridge.stopping_runtime is None and bridge.runtime_message.startswith((
                        'Stop unconfirmed', 'Manager movement stop unconfirmed',
                        'Action stop unconfirmed')):
                    raise OperationError('stop_unconfirmed', bridge.runtime_message)
                return False
            self._wait(stopped, canceled)
            return {'runtime': bridge.data.snapshot()['runtime']}
        payload = ({'mode': 'navigation', 'map': arguments['map']}
                   if operation == 'map_select' else dict(arguments))
        payload['command'] = 'bringup_start'
        before = bridge.data.snapshot()
        was_stopped = before['runtime']['state'] == 'STOPPED'
        binding = {'runtime_id': arguments.get('movement_runtime_id'),
                   'epoch': arguments.get('movement_epoch')}
        if not was_stopped and not binding['runtime_id']:
            raise OperationError('movement_state_unknown',
                                 'Existing runtime preparation requires its observed epoch')
        payload['_movement_binding'] = (binding['runtime_id'], binding['epoch'])
        dispatch_started = threading.Event()

        def start():
            self._check_cancel(canceled)
            dispatch_started.set()
            return bridge._start_runtime(payload)

        try:
            future = bridge.call(start)
            if future is not None:
                response = self._wait_ros(future, canceled)
                ok = response.success if hasattr(response, 'success') else response.result == 0
                if not ok:
                    raise OperationError(
                        getattr(response, 'code', 'localization_failed'),
                        'Localization switch was rejected or failed')
            else:
                def ready():
                    runtime = bridge.data.snapshot()['runtime']
                    if runtime['state'] == 'ERROR':
                        raise OperationError(
                            'runtime_failed', runtime.get('message', 'Runtime failed'))
                    return runtime.get('ready')
                self._wait(ready, canceled)
            self._check_cancel(canceled)
            bridge.call(bridge._refresh)

            def localized():
                runtime = bridge.data.snapshot()['runtime']
                state = runtime.get('localization') or {}
                if state.get('mode') == 'ERROR':
                    raise OperationError('localization_failed', state.get('message', 'Failed'))
                if not runtime.get('ready'):
                    return False
                if payload['mode'] == 'mapping':
                    # Baseline startup uses the default unknown map. AutoSLAM
                    # alone switches to SLAM after its mission owns BASE.
                    return state.get('mode') in {'LOCALIZATION', 'MAPPING'}
                return (state.get('mode') == 'LOCALIZATION'
                        and Path(state.get('map') or '').name == payload['map'])
            self._wait(localized, canceled)
            state = self.status()
            system = state.get('system') or {}
            current = {'runtime_id': system.get('movement_runtime_id'),
                       'epoch': system.get('movement_epoch')}
            if was_stopped:
                # A new Manager always starts at zero. Adopting a newer epoch
                # here would resume a request stopped during startup.
                binding = {'runtime_id': current['runtime_id'], 'epoch': 0}
            if not binding['runtime_id'] or type(binding['epoch']) is not int:
                raise OperationError('movement_state_unknown', 'Manager state is unavailable')
            if binding != current:
                raise OperationError('movement_epoch_changed',
                                     'Movement was stopped during preparation')
            runtime = state['runtime']
            localization = runtime.get('localization') or {}
            if payload['mode'] == 'navigation':
                if localization.get('map') != payload['map']:
                    raise OperationError(
                        'localization_unconfirmed', 'Selected map has not been observed')
            return {'runtime': runtime, 'localization': localization, 'map': payload.get('map'),
                    'preparation_movement_binding': binding}
        except Exception as error:
            started = (dispatch_started.is_set()
                       or isinstance(error, OwnerCallTimeout) and error.started)
            if started and (
                    canceled.is_set() or isinstance(error, TimeoutError)
                    or isinstance(error, OperationError) and error.code == 'result_unknown'):
                # Interrupted preparation must not leave an owned runtime to
                # later launch its initial localization movement unobserved.
                try:
                    if was_stopped and bridge.runtime:
                        cleanup = bridge.call(lambda: bridge.runtime.stop(), timeout=65.0)
                    else:
                        def stop_localization():
                            if not bridge.stop_movement.service_is_ready():
                                raise RuntimeError('Movement stop service is unavailable')
                            return bridge.stop_movement.call_async(bridge.stop_movement_request(
                                request_id='prepare-cancel-' + str(time.time_ns())))
                        cleanup = bridge.call(stop_localization, timeout=65.0)
                    # Cleanup must finish even though the original operation's
                    # cancellation flag is already set.
                    self._wait(cleanup.done, threading.Event(), timeout=65.0)
                    stopped = cleanup.result()
                    if not was_stopped and not stopped.stopped:
                        raise RuntimeError('Movement stop is unconfirmed')
                except Exception as cleanup_error:
                    raise OperationError(
                        'stop_unconfirmed',
                        'Interrupted preparation stop is unconfirmed') from cleanup_error
            if canceled.is_set():
                raise OperationError(
                    'canceled', 'Preparation canceled before further effects') from error
            if isinstance(error, TimeoutError):
                raise OperationError(
                    'result_unknown' if started else 'dispatch_timeout',
                    'Preparation dispatch timed out') from error
            raise

    def _zones(self):
        view = zone_view(self.bridge.data.snapshot()['runtime'], self.bridge.catalog)
        view['revision'] = hashlib.sha256(json.dumps(view, sort_keys=True).encode()).hexdigest()
        return view


class DeviceOperationServer:
    """Keep action callbacks responsive while workers wait on ROS and HTTPS."""

    def __init__(self, bridge, operations):
        from malbut_interfaces.action import DeviceOperation
        from rclpy.action import ActionServer, CancelResponse, GoalResponse
        from rclpy.qos import DurabilityPolicy, QoSProfile
        from std_msgs.msg import String
        self.bridge, self.operations = bridge, operations
        self.action = DeviceOperation
        self.workers = ThreadPoolExecutor(max_workers=3, thread_name_prefix='device-operation')
        self.pending = {}
        self.server = ActionServer(
            bridge.node, DeviceOperation, '/malbut/device/operate', self._execute,
            goal_callback=lambda goal: GoalResponse.ACCEPT if (
                goal.operation in OPERATIONS and len(goal.arguments_json) <= 16384
                and len(self.pending) < 8) else GoalResponse.REJECT,
            cancel_callback=lambda _: CancelResponse.ACCEPT)
        self.publisher = bridge.node.create_publisher(
            String, '/malbut/device/state', QoSProfile(
                depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))
        self.string = String
        self.timer = bridge.node.create_timer(0.1, self._poll)
        self.published_at = 0.0

    async def _execute(self, handle):
        from rclpy.task import Future
        completion = Future()
        canceled = threading.Event()
        request = handle.request
        handle.publish_feedback(self.action.Feedback(
            state='running', message='Operation admitted'))
        try:
            arguments = json.loads(request.arguments_json or '{}')
        except ValueError:
            arguments = None
        work = self.workers.submit(self.operations.execute, request.request_id,
                                   request.operation, arguments, canceled)
        self.pending[id(handle)] = (handle, work, completion, canceled)
        response = await completion
        if response['code'] == 'canceled' and handle.is_cancel_requested:
            handle.canceled()
        elif response['success']:
            handle.succeed()
        else:
            handle.abort()
        return self.action.Result(success=response['success'], code=response['code'],
                                  result_json=json.dumps(response['result'], ensure_ascii=False),
                                  message=response['message'])

    def _poll(self):
        for key, (handle, work, completion, canceled) in list(self.pending.items()):
            if handle.is_cancel_requested:
                canceled.set()
            if work.done():
                del self.pending[key]
                completion.set_result(work.result())
        if time.monotonic() - self.published_at >= 1.0:
            self.published_at = time.monotonic()
            # No file/network queries on the ROS executor: cached bridge state
            # supplies continuous status, richer catalog queries are operations.
            state = self.bridge.data.snapshot()
            for field in ('log_path', 'log_tail'):
                state['runtime'].pop(field, None)
            self.publisher.publish(self.string(data=json.dumps(state)))

    def close(self):
        """Prevent later effects before shutting down the owning bridge."""
        for _, _, _, canceled in self.pending.values():
            canceled.set()
        self.workers.shutdown(wait=True, cancel_futures=True)
        self.server.destroy()
        self.operations.close()
