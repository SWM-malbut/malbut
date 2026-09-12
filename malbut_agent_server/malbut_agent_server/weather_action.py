"""Serve Manager-dispatched weather queries and saved location settings."""

import math
from pathlib import Path
import sys
from threading import Event, RLock, Thread
import time
from urllib.error import URLError

import yaml

from malbut_agent_server.config import load_env_file
from malbut_agent_server.weather import WeatherState
from malbut_agent_server.weather_kma import KmaWeatherClient, KmaWeatherError
from malbut_agent_server.weather_location_store import (
    DEFAULT_WEATHER_LOCATION_PATH, WeatherLocationStore, resolve_weather_location,
)


WEATHER_ACTION = '/malbut/weather/get'
SET_LOCATION_ACTION = '/malbut/weather/location/set'


def _location_query(request):
    if (request.capability_id != 'set_weather_location'
            or len(request.arguments_yaml.encode('utf-8')) > 1024):
        raise ValueError('invalid location request')
    value = yaml.safe_load(request.arguments_yaml)
    if not isinstance(value, dict) or set(value) != {'location'}:
        raise ValueError('invalid location arguments')
    query = value['location']
    if (not isinstance(query, str) or not query.strip() or len(query) > 120
            or any(ord(char) < 32 for char in query)):
        raise ValueError('invalid location query')
    return query.strip()


def _message(state):
    from malbut_interfaces.msg import WeatherForecast, WeatherState

    message = WeatherState()
    for name in ('fetched_at', 'valid_at'):
        value = getattr(state, name)
        seconds = math.floor(value)
        nanos = round((value - seconds) * 1_000_000_000)
        stamp = getattr(message, name)
        carry, stamp.nanosec = divmod(nanos, 1_000_000_000)
        stamp.sec = seconds + carry
    for name in ('location', 'latitude', 'longitude', 'source', 'timezone',
                 'temperature_c', 'weather_code'):
        setattr(message, name, getattr(state, name))
    for forecast in state.daily:
        item = WeatherForecast()
        for name in ('date', 'temperature_max_c', 'temperature_min_c',
                     'precipitation_probability_max_pct', 'weather_code'):
            setattr(item, name, getattr(forecast, name))
        message.daily.append(item)
    return message


def create_weather_action_node(*, client=None, timeout_s=10.0,
                               location_store=None, location_resolver=None):
    """Create a configured Action server without making an HTTP request."""
    from malbut_interfaces.action import ExecuteMission, GetWeather
    from rclpy.action import ActionServer, CancelResponse, GoalResponse
    from rclpy.callback_groups import ReentrantCallbackGroup
    from rclpy.node import Node

    if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
            or not math.isfinite(timeout_s) or timeout_s <= 0):
        raise ValueError('weather Action timeout must be positive')

    class WeatherActionNode(Node):
        def __init__(self):
            super().__init__('malbut_weather')
            self._lock = RLock()
            self._closed = Event()
            self._active = False
            self._finishing = False
            self._cancel_requested = False
            self._worker = None
            self._server = None
            self._location_server = None
            self._store = location_store
            self._owns_store = location_store is None
            self._resolve_location = location_resolver or resolve_weather_location
            try:
                database_path = self.declare_parameter(
                    'weather_location_path', DEFAULT_WEATHER_LOCATION_PATH,
                ).value
                if self._store is None:
                    self._store = WeatherLocationStore(
                        ':memory:' if client is not None else database_path,
                    )
                defaults = {
                    'location': '', 'latitude': float('nan'),
                    'longitude': float('nan'), 'timezone': 'Asia/Seoul',
                }
                values = {name: self.declare_parameter(name, default).value
                          for name, default in defaults.items()}
                automatic = values['location'] == '' and all(
                    isinstance(values[name], float) and math.isnan(values[name])
                    for name in ('latitude', 'longitude')
                )
                self._client = client
                if client is None and not automatic:
                    self._client = KmaWeatherClient(**values)
                self._server = ActionServer(
                    self, GetWeather, WEATHER_ACTION,
                    execute_callback=self._execute,
                    goal_callback=self._goal,
                    cancel_callback=self._cancel,
                    callback_group=ReentrantCallbackGroup(),
                )
                self._location_server = ActionServer(
                    self, ExecuteMission, SET_LOCATION_ACTION,
                    execute_callback=lambda handle: self._execute(handle, setting=True),
                    goal_callback=self._location_goal,
                    cancel_callback=self._cancel,
                    callback_group=ReentrantCallbackGroup(),
                )
            except Exception:
                self.destroy_node()
                raise

        def _goal(self, _request):
            with self._lock:
                if (self._closed.is_set() or self._active
                        or (self._worker is not None and self._worker.is_alive())):
                    return GoalResponse.REJECT
                self._active = True
                self._finishing = False
                self._cancel_requested = False
                return GoalResponse.ACCEPT

        def _location_goal(self, request):
            try:
                _location_query(request)
            except (ValueError, TypeError, AttributeError, yaml.YAMLError):
                return GoalResponse.REJECT
            return self._goal(request)

        def _cancel(self, handle):
            with self._lock:
                if not handle.is_active or not self._active or self._finishing:
                    return CancelResponse.REJECT
                self._cancel_requested = True
                return CancelResponse.ACCEPT

        def _execute(self, handle, *, setting=False):
            done = Event()
            outcome = GetWeather.Result()
            candidates = []
            deadline = time.monotonic() + timeout_s

            def fetch():
                try:
                    if setting:
                        candidates.extend(self._resolve_location(_location_query(handle.request)))
                        return
                    client = self._client
                    location = self._store.get()
                    if location is not None:
                        client = KmaWeatherClient(**location)
                    elif client is None:
                        outcome.error_code = 'LOCATION_REQUIRED'
                        return
                    with self._lock:
                        if (self._closed.is_set() or self._cancel_requested
                                or handle.is_cancel_requested
                                or time.monotonic() >= deadline):
                            return
                    state = client.fetch()
                    if not isinstance(state, WeatherState):
                        raise ValueError('invalid weather snapshot')
                    outcome.weather = _message(state)
                except KmaWeatherError as error:
                    outcome.error_code = error.code
                except TimeoutError:
                    outcome.error_code = 'TIMEOUT'
                except URLError as error:
                    outcome.error_code = (
                        'TIMEOUT' if isinstance(error.reason, TimeoutError)
                        else 'FETCH_FAILED'
                    )
                except (ValueError, TypeError, AttributeError, OverflowError):
                    outcome.error_code = 'INVALID_DATA'
                except Exception:
                    outcome.error_code = 'FETCH_FAILED'
                finally:
                    done.set()

            try:
                with self._lock:
                    if not self._closed.is_set() and not self._cancel_requested:
                        feedback = (ExecuteMission.Feedback(state='RESOLVING') if setting
                                    else GetWeather.Feedback(state='FETCHING'))
                        handle.publish_feedback(feedback)
                        self._worker = Thread(target=fetch, daemon=True)
                        self._worker.start()
                while True:
                    with self._lock:
                        if handle.is_cancel_requested:
                            code = 'CANCELED'
                        elif self._closed.is_set():
                            code = 'SHUTTING_DOWN'
                        elif self._cancel_requested:
                            # rclpy enters CANCELING after the cancel callback returns.
                            code = None
                        elif time.monotonic() >= deadline:
                            code = 'TIMEOUT'
                        elif done.is_set():
                            code = outcome.error_code
                        else:
                            code = None
                        if code is not None:
                            self._finishing = True
                            if setting and not code:
                                # Commit only after a timely resolver result. A late worker
                                # can never mutate the saved location after cancellation.
                                try:
                                    if len(candidates) == 1:
                                        saved = self._store.set(candidates[0])
                                        payload = {'status': 'location_set',
                                                   'location': saved['location']}
                                    elif candidates:
                                        payload = {
                                            'status': 'location_ambiguous',
                                            'candidates': [item['location']
                                                           for item in candidates[:5]],
                                        }
                                    else:
                                        payload = {'status': 'location_not_found'}
                                except Exception:
                                    code = 'LOCATION_SAVE_FAILED'
                            break
                    if self._cancel_requested:
                        self._closed.wait(0.01)
                    else:
                        done.wait(0.05)
                if code:
                    result = (ExecuteMission.Result(
                        result_yaml=yaml.safe_dump({'status': 'unavailable'}), message=code,
                    ) if setting else GetWeather.Result(error_code=code, message=code))
                    if code == 'CANCELED':
                        handle.canceled()
                    else:
                        handle.abort()
                    return result
                handle.succeed()
                if setting:
                    return ExecuteMission.Result(
                        result_yaml=yaml.safe_dump(payload, allow_unicode=True),
                    )
                return outcome
            finally:
                with self._lock:
                    self._active = False

        def close(self):
            """Stop accepting goals and release execution before executor shutdown."""
            with self._lock:
                self._closed.set()

        def destroy_node(self):
            self.close()
            if self._server is not None:
                self._server.destroy()
                self._server = None
            if self._location_server is not None:
                self._location_server.destroy()
                self._location_server = None
            if self._owns_store and self._store is not None:
                self._store.close()
                self._store = None
            return super().destroy_node()

    return WeatherActionNode()


def main(args=None):
    """Keep cancellation callbacks responsive while one HTTP worker runs."""
    try:
        import rclpy
        from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
    except ImportError:
        print('ROS 2 rclpy is required; source the ROS environment.', file=sys.stderr)
        return 2
    node = executor = None
    initialized = False
    try:
        load_env_file(Path('.env'))
        rclpy.init(args=args)
        initialized = True
        node = create_weather_action_node()
        executor = MultiThreadedExecutor(num_threads=2)
        executor.add_node(node)
        executor.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    except Exception as error:
        print('Weather Action stopped: ' + type(error).__name__, file=sys.stderr)
        return 2
    finally:
        if node is not None:
            node.close()
        if executor is not None:
            executor.shutdown()
        if node is not None:
            node.destroy_node()
        if initialized and rclpy.ok():
            rclpy.shutdown()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
