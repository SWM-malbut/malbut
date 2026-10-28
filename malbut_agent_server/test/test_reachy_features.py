"""Local transports and real orchestration; never call an API or open a camera."""
from dataclasses import replace
from types import SimpleNamespace
import json
import pytest

from malbut_agent_server.camera_view import LatestCameraView, OpenAICameraAnswerer
from malbut_agent_server.config import Settings
from malbut_agent_server.factory import build_orchestrator
from malbut_agent_server.information_queries import RobotGuide, configure_information_queries
from malbut_agent_server.schemas import AgentDecision, AgentRequest, ProviderResult, RobotState
from malbut_agent_server.speech_metrics import SpeechMetrics


def test_worker_emits_correlated_metadata_without_recording_speech_text():
    import time
    from malbut_agent_server.speech_dialogue import DialogueWorker
    logs = []
    worker = DialogueWorker(lambda: build_orchestrator(
        Settings(database_path=':memory:'), http_server=False), 'owner',
        logger=SimpleNamespace(info=logs.append))
    try:
        assert worker.submit('trace-1', '비공개 대화 문장')
        deadline = time.monotonic() + 3
        while not worker.drain():
            assert time.monotonic() < deadline
            time.sleep(.002)
        events = [json.loads(value) for value in logs]
        assert [item['stage'] for item in events] == [
            'agent_queued', 'agent_started', 'agent_finished']
        assert all(item['request_id'] == 'trace-1' for item in events)
        assert '비공개' not in ''.join(logs)
    finally:
        worker.close()


def test_camera_needs_fresh_enabled_frame_and_rechecks_before_sending():
    now, enabled, calls = [10.], [True], []
    frame = SimpleNamespace(width=1, height=1, step=3, encoding='rgb8', data=b'\xff\x00\x00')
    view = LatestCameraView(SimpleNamespace(answer=lambda q, b: calls.append((q, b)) or '빨간 물체예요.'),
        enabled=lambda: enabled[0], clock=lambda: now[0], encoder=lambda _: b'jpeg')
    with pytest.raises(ValueError): view.answer('뭐야?')
    view.update(frame)
    assert view.answer('뭐야?') == '빨간 물체예요.'
    now[0] = 11.01
    with pytest.raises(ValueError): view.answer('뭐야?')
    view.update(frame, source_age_s=3)
    with pytest.raises(ValueError): view.answer('뭐야?')
    view.update(frame)
    enabled[0] = False
    with pytest.raises(ValueError): view.answer('뭐야?')
    assert len(calls) == 1


@pytest.mark.parametrize('output', [
    [{'type': 'function_call', 'name': 'request_navigation'}],
    [{'type': 'message', 'content': [{'type': 'refusal', 'refusal': 'no'}]}],
])
def test_camera_rejects_non_answer_output(output):
    client = OpenAICameraAnswerer('offline-fixture', 'configured-model', 'https://api.openai.com/v1',
        transport=lambda *args: {'status': 'completed', 'output': output})
    with pytest.raises(ValueError): client.answer('뭐야?', b'jpeg')


def test_camera_transport_reuses_key_on_each_request_without_tools_or_history():
    keys, requests = ['offline-first', 'offline-second'], []
    key = SimpleNamespace(current=lambda: keys.pop(0))
    def transport(url, headers, payload, timeout):
        requests.append((headers['Authorization'], payload, timeout))
        return {'status': 'completed', 'output': [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': '컵이 보여요.'}]}]}
    client = OpenAICameraAnswerer(key, 'configured-model', 'https://api.openai.com/v1', transport=transport)
    assert client.answer('앞에 뭐가 있어?', b'jpeg') == '컵이 보여요.'
    client.answer('컵 색은?', b'jpeg')
    assert [row[0] for row in requests] == ['Bearer offline-first', 'Bearer offline-second']
    for _, payload, timeout in requests:
        assert payload['store'] is False and 'tools' not in payload
        assert len(payload['input']) == 1 and timeout <= 10
        assert payload['input'][0]['content'][1]['type'] == 'input_image'


@pytest.mark.parametrize('query,title', [
    ('순찰은 어떻게 사용해?', '순찰'), ('거실로 이동하는 방법', '목적지로 이동'),
    ('말투를 짧게 바꿀 수 있어?', '기억과 말투'), ('내일 날씨도 알려줘?', '날씨'),
    ('사람을 따라오게 하려면?', '사람 따라가기'), ('카메라로 볼 수 있어?', '카메라와 순찰 기록'),
])
def test_packaged_guide_search(query, title):
    assert RobotGuide().search(query)[0]['title'] == title


@pytest.mark.parametrize('query', [
    '순찰 기능 사용 방법과 시작 조건, 중지 방법',
    '순찰 사용법 주방만 지정 한 시간 계속 순찰 지원 여부',
])
def test_patrol_search_does_not_append_camera_history_for_shared_word(query):
    answer = RobotGuide().answer(query)
    assert '저장된 지도' in answer
    assert '카메라' not in answer and '관찰 시각' not in answer


def test_camera_query_instructions_do_not_invent_camera_state_or_other_capabilities():
    from malbut_agent_server.prompting import prepare_model_input, system_instructions_for_tools
    from malbut_agent_server.tools import TOOL_SPECS
    instructions = system_instructions_for_tools([TOOL_SPECS['describe_camera_view']])
    assert '실제 프레임 유무·최신성은 도구가 검증' in instructions
    assert 'privacy_mode가 true이면 조회하지 않고' in instructions
    assert 'describe_camera_view 조회가 연결' not in system_instructions_for_tools([])
    assert 'search_robot_docs로 설치된 안내' not in instructions
    request = AgentRequest(request_id='r', user_id='u', conversation_id='c', turn_id='t',
        utterance='앞에 뭐가 있어?', robot_state=RobotState(),
        available_tools=('describe_camera_view',))
    prepared = prepare_model_input(request, [], [], None, 20000, 10)
    data = json.loads(prepared.text.split('\n', 1)[1])
    assert data['robot_state_untrusted']['camera_available'] is False


class QueryProvider:
    def __init__(self, tool, arguments):
        self.tool, self.arguments = tool, arguments
    def complete(self, request, memories, turns, tools, conversation_summary=None, **kwargs):
        return ProviderResult(AgentDecision('tool_call', '확인', tool_name=self.tool,
            arguments=self.arguments), 'local-fixture', 'fixed', 1.)


@pytest.mark.parametrize('tool,args,answer', [
    ('search_robot_docs', {'query': '순찰 사용법'}, '저장된 지도'),
    ('describe_camera_view', {'question': '지금 뭐가 보여?'}, '컵이 보여요.'),
])
def test_real_orchestrator_answers_once_without_mission_or_memory_write(tool, args, answer):
    runtime = build_orchestrator(Settings(database_path=':memory:'), http_server=False)
    calls = []
    camera = SimpleNamespace(answer=lambda q: calls.append(q) or '컵이 보여요.')
    configure_information_queries(runtime, guide=RobotGuide(), camera=camera)
    runtime.provider = QueryProvider(tool, args)
    runtime.conversation_store.create('owner', 'chat')
    request = AgentRequest(request_id='q1', user_id='owner', conversation_id='chat',
        turn_id='t1', utterance='질문', robot_state=RobotState(), available_tools=(tool,))
    try:
        result = runtime.handle(request)
        assert result.decision.type == 'message' and answer in result.decision.message
        assert result.safety.code == 'information_read_only'
        assert result.provider_result.memory_proposal is None
        runtime.handle(request)
        assert len(calls) == (1 if tool == 'describe_camera_view' else 0)
    finally:
        runtime.close()


def test_missing_camera_is_not_advertised_or_executed():
    runtime = build_orchestrator(Settings(database_path=':memory:'), http_server=False)
    try:
        assert runtime.information_query_tools == ('search_robot_docs',)
        assert not runtime.capability_registry.get('describe_camera_view').available
    finally:
        runtime.close()


def test_metrics_ignore_interim_duplicate_playing_and_keep_missing_stages_null():
    m = SpeechMetrics(capacity=2)
    def event(stage, stamp, **fields):
        return dict(event='speech_timing', request_id='turn', stage=stage, at_s=stamp, **fields)
    m.observe(event('endpoint', 2., estimated_speech_end_s=1.))
    m.observe(event('transcript_final', 2.1))
    m.observe(event('agent_queued', 2.2))
    m.observe(event('agent_started', 2.4))
    m.observe(event('agent_finished', 3.))
    m.observe(event('playback_started', 2.5, playback_id='progress', interim=True))
    m.observe(event('tts_started', 3.1, playback_id='final', interim=False))
    m.observe(event('tts_first_audio', 3.2, playback_id='final', interim=False))
    m.observe(event('playback_started', 3.6, playback_id='final', interim=False))
    m.observe(event('playback_started', 9., playback_id='final', interim=False))
    row, = m.summaries()
    assert row['speech_to_playback_ms'] == 2600
    assert row['agent_queue_ms'] == 200 and row['agent_ms'] == 600
    assert row['tts_first_audio_ms'] == 100 and row['audio_to_playback_ms'] == 400
    m.observe(dict(event('agent_started', 4.), request_id='incomplete'))
    assert m.summaries()[-1]['speech_to_playback_ms'] is None
    m.observe(dict(event('agent_started', 5.), request_id='new'))
    assert len(m.turns) == 2 and 'turn' not in m.turns
