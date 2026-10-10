"""Weather tools through the local console with offline observations only."""

from datetime import datetime, timedelta
import time
from zoneinfo import ZoneInfo

import test_agent_console_support  # noqa: F401

from console_core import ConsoleCore
from malbut_agent_server.config import Settings
from malbut_agent_server.mission_audio import CATALOG
from malbut_agent_server.providers.base import AgentProvider
from malbut_agent_server.schemas import AgentDecision, ProviderResult
from malbut_agent_server.weather import SOURCE, WeatherForecast, WeatherState
from malbut_agent_server.weather_kma import KmaWeatherClient, KmaWeatherError


class WeatherProvider(AgentProvider):
    def __init__(self):
        self.calls = []
        self.location = None

    def complete(self, request, memories, conversation_turns, tools,
                 conversation_summary=None, *, weather_context=None):
        self.calls.append(([tool.name for tool in tools], weather_context))
        if weather_context is not None:
            decision = AgentDecision('message', weather_context['status'])
        else:
            decision = AgentDecision(
                'tool_call', '',
                tool_name='set_weather_location' if self.location else 'get_weather',
                arguments={'location': self.location} if self.location else {},
            )
        return ProviderResult(decision, 'offline-weather', 'fixture', 0)


def test_chat_selects_weather_saves_location_fetches_and_preserves_location(tmp_path, monkeypatch):
    fetches = []

    def fetch(client):
        assert client._service_key == 'explicit-test-key'
        fetches.append(client.location)
        now = time.time()
        today = datetime.fromtimestamp(now, ZoneInfo('Asia/Seoul')).date()
        return WeatherState(
            fetched_at=now, valid_at=now, location=client.location,
            latitude=client.latitude, longitude=client.longitude,
            source=SOURCE, timezone='Asia/Seoul', temperature_c=23, weather_code=0,
            daily=tuple(WeatherForecast((today + timedelta(days=offset)).isoformat(),
                                        25, 17, 10, 0) for offset in range(2)),
        )

    monkeypatch.setattr(KmaWeatherClient, 'fetch', fetch)
    settings = Settings(database_path=str(tmp_path / 'agent.sqlite3'), tool_mode='simulation')
    core = ConsoleCore(settings, weather_service_key='explicit-test-key')
    provider = WeatherProvider()
    core.runtime.provider = provider
    try:
        assert fetches == []
        missing = core.chat('오늘 날씨가 어때?')
        assert missing['text'] == CATALOG['weather.location_required']
        assert missing['metadata']['decision']['reason'] == 'weather.location_required'
        assert fetches == []
        assert provider.calls == [
            (['get_weather', 'set_weather_location'], None),
        ]
        provider.location = '수원 우만동'
        ambiguous = core.chat('수원 우만동에 있어')
        assert ambiguous['metadata']['decision']['reason'] == 'weather_location_ambiguous'
        assert core.weather.store.get() is None
        provider.location = '서울'
        saved = core.chat('서울에 있어')
        assert saved['metadata']['decision']['reason'] == 'weather_location_saved'
        assert core.weather.store.get()['location'] == '서울특별시'
        provider.location = None
        answer = core.chat('오늘 날씨가 어때?')
        assert answer['text'] == 'fresh'
        assert answer['physical_authorized'] is False
        assert answer['metadata']['execution']['authorized'] is False
        assert provider.calls[-1][0] == []
        assert provider.calls[-1][1]['current']['temperature_c'] == 23
        assert fetches == ['서울특별시']
    finally:
        core.close()
    reopened = ConsoleCore(settings)
    try:
        assert reopened.weather.store.get()['location'] == '서울특별시'
    finally:
        reopened.close()


def test_direct_weather_tools_validate_inputs_and_report_real_connection(monkeypatch):
    monkeypatch.setenv('KMA_SERVICE_KEY', 'unselected-test-key')
    monkeypatch.setattr(KmaWeatherClient, '_request',
                        lambda *args: (_ for _ in ()).throw(AssertionError('unexpected network')))
    core = ConsoleCore(Settings(tool_mode='simulation'))
    try:
        tools = {item['name']: item for item in core.tools()['capabilities']}
        for name in ('get_weather', 'set_weather_location'):
            assert tools[name]['console_status'] == 'local_weather'
            assert tools[name]['executable'] is True
            assert tools[name]['blocked_by'] is None
        assert tools['navigate']['console_status'] == 'simulation'
        assert core.query_tool('get_weather', {'location': '서울'})['error']['code'] == 'invalid_arguments'
        assert core.query_tool('set_weather_location', {})['error']['code'] == 'invalid_arguments'
        unknown = core.query_tool('set_weather_location', {'location': '없는시험지역'})
        assert unknown['result'] == {'status': 'location_not_found'}
        assert core.query_tool('set_weather_location', {'location': '서울'})['result']['status'] == 'location_set'
        missing_key = core.query_tool('get_weather', {})
        assert missing_key['status'] == 'failed'
        assert missing_key['result'] == {'status': 'unavailable', 'error_code': 'KMA_KEY_REQUIRED'}
        assert missing_key['physical_authorized'] is False
    finally:
        core.close()


def test_weather_api_error_is_bounded_and_does_not_break_console(monkeypatch):
    def fetch(client):
        raise KmaWeatherError('KMA_UNAVAILABLE')

    monkeypatch.setattr(KmaWeatherClient, 'fetch', fetch)
    core = ConsoleCore(Settings(tool_mode='simulation'))
    try:
        core.query_tool('set_weather_location', {'location': '서울'})
        failed = core.query_tool('get_weather', {})
        assert failed['status'] == 'failed'
        assert failed['error']['code'] == 'KMA_UNAVAILABLE'
        assert core.chat('안녕')['text']
    finally:
        core.close()
