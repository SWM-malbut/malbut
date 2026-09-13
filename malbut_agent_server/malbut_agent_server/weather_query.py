"""Bridge dialogue tool calls to Manager on the owning ROS thread."""

from concurrent.futures import CancelledError
from dataclasses import dataclass, field
import math
from threading import Event, RLock

from malbut_agent_server.weather import decode_weather_result


_PREFIX = 'weather-query:'


@dataclass
class _Pending:
    capability_id: str = 'get_weather'
    arguments: dict = field(default_factory=dict)
    ready: Event = field(default_factory=Event)
    submitted: bool = False
    result: dict | None = None


class ManagerWeatherQuery:
    """Wait in the dialogue worker; submit and observe only in ROS callbacks."""

    def __init__(self, manager, *, timeout_s=20.0):
        if (isinstance(timeout_s, bool) or not math.isfinite(timeout_s)
                or timeout_s <= 0):
            raise ValueError('weather query timeout must be positive')
        self._manager = manager
        self._timeout_s = timeout_s
        self._lock = RLock()
        self._pending = {}
        self._cancels = set()
        self._closed = False

    def execute(self, request_id):
        """Return only the result of one matching successful Manager mission."""
        return self._execute(request_id, 'get_weather', {})

    def set_location(self, request_id, location):
        """Persist an explicitly supplied location through Manager."""
        if (not isinstance(location, str) or not location.strip()
                or len(location) > 120 or any(ord(char) < 32 for char in location)):
            raise ValueError('invalid weather location')
        import yaml

        return self._execute(request_id, 'set_weather_location', {
            'arguments_yaml': yaml.safe_dump({'location': location.strip()}, allow_unicode=True),
        })

    def _execute(self, request_id, capability_id, arguments):
        key = _PREFIX + ('set:' if capability_id == 'set_weather_location' else '') + request_id
        pending = _Pending(capability_id=capability_id, arguments=arguments)
        with self._lock:
            if self._closed:
                raise CancelledError('weather query is closed')
            if key in self._pending:
                raise RuntimeError('weather query is unavailable')
            self._pending[key] = pending
        try:
            ready = pending.ready.wait(self._timeout_s)
            with self._lock:
                if self._closed:
                    raise CancelledError('weather query is closed')
                if not ready:
                    if pending.submitted:
                        self._cancels.add(key)
                    raise TimeoutError('Manager weather query timed out')
                result = pending.result
            if result is None or result.get('kind') not in {'succeeded', 'failed'}:
                raise RuntimeError('Manager weather query did not succeed')
            raw = result.get('result_yaml')
            if not isinstance(raw, str) or len(raw.encode('utf-8')) > 16384:
                raise ValueError('invalid Manager weather result')
            import yaml

            value = yaml.safe_load(raw)
            if capability_id == 'get_weather':
                if (result['kind'] == 'failed' and result.get('ros_status') == 6
                        and isinstance(value, dict)
                        and set(value) == {'weather', 'error_code', 'message'}
                        and value['error_code'] == 'LOCATION_REQUIRED'):
                    return {'status': 'location_required'}
                if result['kind'] != 'succeeded':
                    raise RuntimeError('Manager weather query did not succeed')
                return decode_weather_result(value)
            if (result['kind'] != 'succeeded' or not isinstance(value, dict)
                    or set(value) != {'mission_id', 'result_yaml', 'message'}
                    or not isinstance(value['result_yaml'], str)):
                raise ValueError('invalid location setting result')
            context = yaml.safe_load(value['result_yaml'])
            if not isinstance(context, dict):
                raise ValueError('invalid location setting result')
            status = context.get('status')
            if status == 'location_set':
                if set(context) != {'status', 'location'} or not _label(context['location']):
                    raise ValueError('invalid saved location')
            elif status == 'location_ambiguous':
                candidates = context.get('candidates')
                if (set(context) != {'status', 'candidates'}
                        or not isinstance(candidates, list) or not 2 <= len(candidates) <= 5
                        or not all(_label(item) for item in candidates)):
                    raise ValueError('invalid location candidates')
            elif status != 'location_not_found' or set(context) != {'status'}:
                raise ValueError('invalid location setting status')
            return context
        finally:
            with self._lock:
                self._pending.pop(key, None)

    def drain(self):
        """Submit queued calls while the ROS executor owns graph access."""
        with self._lock:
            for key in tuple(self._cancels):
                self._cancels.remove(key)
                try:
                    self._manager.cancel(key)
                except Exception:
                    pass  # Cancellation is best effort; never resubmit a Goal.
            if self._closed:
                return
            for key, pending in tuple(self._pending.items()):
                if pending.submitted or pending.ready.is_set():
                    continue
                pending.submitted = True
                try:
                    self._manager.submit(
                        pending.capability_id, pending.arguments, request_id=key,
                    )
                    self.handle(self._manager.snapshot(key))
                except Exception:
                    pending.ready.set()

    def handle(self, event):
        """Consume weather lifecycle events, including late timed-out replies."""
        key = event.get('request_id', '')
        if (event.get('capability_id') not in {'get_weather', 'set_weather_location'}
                or not key.startswith(_PREFIX)):
            return False
        with self._lock:
            pending = self._pending.get(key)
            if pending is not None and event.get('capability_id') != pending.capability_id:
                return False
            if pending is not None and not pending.ready.is_set():
                if event.get('terminal') or event.get('kind') == 'unknown':
                    pending.result = dict(event)
                    pending.ready.set()
        return True

    def close(self):
        """Release waiting dialogue before its owning thread is joined."""
        with self._lock:
            self._closed = True
            for key, pending in self._pending.items():
                if pending.submitted and not pending.ready.is_set():
                    self._cancels.add(key)
                pending.result = None
                pending.ready.set()


def _label(value):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= 200
            and all(ord(char) >= 32 for char in value))
