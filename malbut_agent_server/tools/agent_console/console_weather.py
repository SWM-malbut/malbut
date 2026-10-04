"""Local console weather execution without ROS or robot actions."""

from malbut_agent_server.weather import context_from_weather
from malbut_agent_server.weather_kma import KmaWeatherClient, KmaWeatherError
from malbut_agent_server.weather_location_store import (
    WeatherLocationStore, resolve_weather_location,
)


class ConsoleWeather:
    def __init__(self, database_path, *, service_key=''):
        self.store = WeatherLocationStore(database_path)
        self._service_key = service_key

    def execute(self, request_id):
        try:
            location = self.store.get()
            if location is None:
                return {'status': 'location_required'}
            client = KmaWeatherClient(**location, service_key=self._service_key)
            return context_from_weather(client.fetch())
        except KmaWeatherError as error:
            return {'status': 'unavailable', 'error_code': error.code}
        except Exception:
            return {'status': 'unavailable', 'error_code': 'FETCH_FAILED'}

    def set_location(self, request_id, location):
        try:
            candidates = resolve_weather_location(location)
            if not candidates:
                return {'status': 'location_not_found'}
            if len(candidates) > 1:
                return {'status': 'location_ambiguous',
                        'candidates': [item['location'] for item in candidates[:5]]}
            saved = self.store.set(candidates[0])
            return {'status': 'location_set', 'location': saved['location']}
        except Exception:
            return {'status': 'unavailable', 'error_code': 'LOCATION_SAVE_FAILED'}

    def close(self):
        self.store.close()
