"""Keep weather available through its public Actions while Manager is stopped."""

from uuid import uuid4

from .weather_query import ManagerWeatherQuery


class ResidentWeatherQuery(ManagerWeatherQuery):
    """Reuse weather result validation with two fixed resident Action endpoints."""

    def __init__(self, node, *, timeout_s=20.0):
        self._actions = _WeatherActions(node, self.handle)
        try:
            super().__init__(self._actions, timeout_s=timeout_s)
        except Exception:
            self._actions.close()
            raise

    def close(self):
        """Release dialogue waits and cancel this adapter's weather goals only."""
        super().close()
        super().drain()
        self._actions.close()


class _WeatherActions:
    """Submit only get_weather and set_weather_location from the ROS thread."""

    def __init__(self, node, on_event):
        from malbut_interfaces.action import ExecuteMission, GetWeather
        from rclpy.action import ActionClient
        from rosidl_runtime_py.convert import message_to_yaml
        from unique_identifier_msgs.msg import UUID

        self._types = {'get_weather': GetWeather, 'set_weather_location': ExecuteMission}
        self._uuid = UUID
        self._serialize = message_to_yaml
        self._on_event = on_event
        self._requests = {}
        self._clients = {}
        self._closed = False
        try:
            for capability, endpoint in (
                    ('get_weather', '/malbut/weather/get'),
                    ('set_weather_location', '/malbut/weather/location/set')):
                self._clients[capability] = ActionClient(node, self._types[capability], endpoint)
        except Exception:
            self.close()
            raise

    def submit(self, capability_id, arguments, request_id):
        if self._closed or capability_id not in self._clients or request_id in self._requests:
            raise RuntimeError('resident weather is unavailable')
        setting = capability_id == 'set_weather_location'
        if (not isinstance(arguments, dict)
                or set(arguments) != ({'arguments_yaml'} if setting else set())
                or setting and not isinstance(arguments['arguments_yaml'], str)):
            raise ValueError('invalid resident weather arguments')
        record = {'request_id': request_id, 'capability_id': capability_id,
                  'kind': 'submitted', 'terminal': False, 'goal_id': uuid4().bytes,
                  'handle': None, 'cancel_requested': False, 'cancel_sent': False}
        self._requests[request_id] = record
        client = self._clients[capability_id]
        if not client.server_is_ready():
            self._finish(record, 'unavailable')
            return
        goal = self._types[capability_id].Goal()
        if setting:
            goal.capability_id = capability_id
            goal.arguments_yaml = arguments['arguments_yaml']
        try:
            future = client.send_goal_async(
                goal, goal_uuid=self._uuid(uuid=list(record['goal_id'])))
            future.add_done_callback(lambda done: self._accepted(record, done))
        except Exception:
            self._finish(record, 'unknown')

    def snapshot(self, request_id):
        record = self._requests.get(request_id)
        return {} if record is None else self._event(record)

    def cancel(self, request_id):
        record = self._requests.get(request_id)
        if record is not None:
            record['cancel_requested'] = True
            self._send_cancel(record)

    def _send_cancel(self, record):
        if record['handle'] is not None and not record['cancel_sent'] and not record['terminal']:
            record['cancel_sent'] = True
            try:
                record['handle'].cancel_goal_async()
            except Exception:
                pass  # A failed cancel never causes a second weather/location goal.

    def _accepted(self, record, future):
        try:
            handle = future.result()
            if (bytes(handle.goal_id.uuid) != record['goal_id']
                    or type(handle.accepted) is not bool):
                raise ValueError('weather goal identity mismatch')
            if not handle.accepted:
                self._finish(record, 'rejected')
                return
            record['handle'] = handle
            if self._closed:
                record['cancel_requested'] = True
            if record['cancel_requested']:
                self._send_cancel(record)
            handle.get_result_async().add_done_callback(lambda done: self._result(record, done))
        except Exception:
            self._finish(record, 'unknown')

    def _result(self, record, future):
        try:
            wrapped = future.result()
            if (type(wrapped.status) is not int or wrapped.status not in (4, 5, 6)
                    or not isinstance(
                        wrapped.result, self._types[record['capability_id']].Result)):
                raise ValueError('invalid weather action result')
            record['ros_status'] = wrapped.status
            record['result_yaml'] = self._serialize(wrapped.result)
            self._finish(record, {4: 'succeeded', 5: 'canceled', 6: 'failed'}[wrapped.status])
        except Exception:
            self._finish(record, 'unknown')

    @staticmethod
    def _event(record):
        keys = ('request_id', 'capability_id', 'kind', 'terminal', 'ros_status', 'result_yaml')
        return {key: value for key, value in record.items() if key in keys}

    def _finish(self, record, kind):
        record.update(kind=kind, terminal=True)
        self._on_event(self._event(record))
        self._requests.pop(record['request_id'], None)

    def close(self):
        if self._closed:
            return
        self._closed = True
        for request_id in tuple(self._requests):
            self.cancel(request_id)
        for client in self._clients.values():
            client.destroy()
