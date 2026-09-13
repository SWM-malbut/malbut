"""Real ROS memory dialogue with fixed inference and no audio or robot use."""

import copy
import time
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest


rclpy = pytest.importorskip('rclpy', reason='ROS 2 is not installed')

from malbut_interfaces.msg import SpeechTranscript  # noqa: E402
from rclpy.executors import SingleThreadedExecutor  # noqa: E402
from rclpy.node import Node  # noqa: E402
from rclpy.qos import (  # noqa: E402
    DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy,
)

from malbut_agent_server.config import Settings  # noqa: E402
from malbut_agent_server import ros_communication  # noqa: E402
from malbut_agent_server.factory import build_orchestrator  # noqa: E402
from malbut_agent_server.memory import SQLiteMemoryStore  # noqa: E402
from malbut_agent_server.personal_memory import CONSENT_PROMPT  # noqa: E402
from malbut_agent_server.ros_communication import (  # noqa: E402
    create_communication_node,
)
from malbut_agent_server.schemas import (  # noqa: E402
    AgentDecision, ProviderResult,
)
from malbut_agent_server.speech_dialogue import (  # noqa: E402
    MEMORY_CHANGED_RESPONSE,
)
from malbut_agent_server.speech_receiver import TRANSCRIPT_TOPIC  # noqa: E402
from malbut_tts import receiver as tts_receiver  # noqa: E402


FACT_TEXT = '우리 강아지 이름은 초코야'
RECALL_TEXT = '강아지 이름이 뭐야?'
RECALL_REPLY = '기억에 따르면 강아지 이름은 초코예요.'
EMPTY_REPLY = '기억된 강아지 이름이 없어요.'
SPEAKER = 'ros-memory-test-user'


class _MemoryProvider:
    supports_memory = True

    def __init__(self):
        self.calls = []

    def complete(
        self, request, memories, conversation_turns, tools,
        conversation_summary=None, *, memory_context=None,
    ):
        assert all(tool.name in {'get_weather', 'set_weather_location'} for tool in tools), (
            'Memory dialogue must not expose robot actuation tools'
        )
        self.calls.append({
            'text': request.utterance,
            'memories': copy.deepcopy(memories),
            'history': copy.deepcopy(conversation_turns),
            'context': copy.deepcopy(memory_context),
        })
        proposal = None
        message = '말씀을 들었어요.'
        if request.utterance == FACT_TEXT:
            proposal = {
                'operation': 'remember',
                'facts': [{
                    'kind': 'pet', 'subject': '강아지', 'attribute': 'name',
                    'value': '초코', 'evidence': request.utterance,
                }],
                'target_ids': [], 'query': '', 'evidence': request.utterance,
            }
        elif request.utterance == RECALL_TEXT:
            message = RECALL_REPLY if any(
                record.metadata.get('fact', {}).get('value') == '초코'
                for record in memories
            ) else EMPTY_REPLY
        elif request.utterance == '강아지 이름 기억 삭제해줘':
            proposal = {
                'operation': 'forget', 'facts': [],
                'target_ids': [
                    record['id'] for record in memory_context['memories']
                    if record.get('fact', {}).get('value') == '초코'
                ],
                'query': '', 'evidence': request.utterance,
            }
        return ProviderResult(
            decision=AgentDecision(type='message', message=message),
            provider='ros-memory-fixture', model='fixed', latency_ms=0.0,
            memory_proposal=proposal, memory_supported=True,
        )


class _DialogueHarness:
    def __init__(self, agent, publisher, executor, received, path, provider):
        self.agent = agent
        self.publisher = publisher
        self.executor = executor
        self.received = received
        self.path = str(path)
        self.provider = provider
        self.sent_ids = []

    def spin_until(self, predicate):
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if predicate():
                return
            self.executor.spin_once(timeout_sec=0.02)
        raise AssertionError('Expected ROS Topic evidence did not arrive')

    def say(self, text):
        previous_count = len(self.received)
        utterance_id = str(uuid4())
        self.sent_ids.append(utterance_id)
        self.publisher.publish(SpeechTranscript(
            utterance_id=utterance_id, text=text,
        ))
        self.spin_until(lambda: len(self.received) > previous_count)
        assert len(self.received) == previous_count + 1
        assert self.agent.missions._requests == {}
        return self.received[-1]

    def inspect_store(self):
        store = SQLiteMemoryStore(self.path)
        try:
            return (
                store.policy_state(SPEAKER),
                list(store.list_for_user(SPEAKER)),
            )
        finally:
            store.close()

    def enable_and_remember(self):
        assert self.say('개인화 켜줘') == CONSENT_PROMPT
        assert '개인화를 시작했어요' in self.say('네')
        assert self.inspect_store()[0]['enabled'] is True
        assert self.say(FACT_TEXT) == '말씀을 들었어요.'
        records = self.inspect_store()[1]
        assert len(records) == 1
        assert records[0].metadata['fact']['value'] == '초코'


@pytest.fixture
def ros_memory(tmp_path, monkeypatch):
    """Use local domain 193, separate stores, and production subscribers."""
    monkeypatch.setenv('ROS_DOMAIN_ID', '193')
    monkeypatch.setenv('ROS_LOCALHOST_ONLY', '1')
    rclpy.init(args=[])
    executor = SingleThreadedExecutor()
    nodes = []
    received = []
    events = []
    provider = _MemoryProvider()
    database = tmp_path / 'dialogue.sqlite3'
    settings = Settings(user_id=SPEAKER, database_path=str(database))
    original_receive = tts_receiver.receive_text
    original_transcript = ros_communication.receive_transcript

    def receive(text, logger):
        result = original_receive(text, logger)
        if result:
            received.append(text)
        return result

    def receive_transcript(receipts, utterance_id, text, logger):
        outcome = original_transcript(receipts, utterance_id, text, logger)
        events.append({
            'utterance_id': utterance_id, 'text': text, 'status': outcome,
        })
        return outcome

    def runtime_factory():
        runtime = build_orchestrator(settings, http_server=False)
        runtime.provider = provider
        return runtime

    monkeypatch.setattr(tts_receiver, 'receive_text', receive)
    monkeypatch.setattr(ros_communication, 'receive_transcript',
                        receive_transcript)
    try:
        def create_agent():
            return create_communication_node(
                speech_db_path=str(tmp_path / 'receipts.sqlite3'),
                dialogue_settings=settings, dialogue_factory=runtime_factory,
            )

        agent = create_agent()
        nodes.append(agent)
        stt = Node('memory_transcript_test_publisher')
        nodes.append(stt)
        tts = tts_receiver.create_receiver_node()
        nodes.append(tts)
        qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST, depth=10,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        )
        publisher = stt.create_publisher(
            SpeechTranscript, TRANSCRIPT_TOPIC, qos,
        )
        for node in nodes:
            executor.add_node(node)
        harness = _DialogueHarness(
            agent, publisher, executor, received, database, provider,
        )
        harness.events = events

        def restart():
            old_agent = harness.agent
            executor.remove_node(old_agent)
            old_agent.destroy_node()
            nodes.remove(old_agent)
            harness.agent = create_agent()
            nodes.append(harness.agent)
            executor.add_node(harness.agent)
            harness.spin_until(lambda: (
                publisher.get_subscription_count() == 1
                and harness.agent._speech.get_subscription_count() == 1
            ))

        harness.restart = restart
        harness.spin_until(lambda: (
            publisher.get_subscription_count() == 1
            and agent._speech.get_subscription_count() == 1
        ))
        yield harness
    finally:
        for node in reversed(nodes):
            executor.remove_node(node)
            node.destroy_node()
        executor.shutdown(timeout_sec=2.0)
        if rclpy.ok():
            rclpy.shutdown()


def test_ros_consent_save_recall_and_delete(ros_memory):
    """Real Topic turns obey consent and reflect confirmed storage changes."""
    assert ros_memory.say(FACT_TEXT) == '말씀을 들었어요.'
    state, records = ros_memory.inspect_store()
    assert state['enabled'] is False
    assert records == []

    ros_memory.enable_and_remember()
    assert ros_memory.say(RECALL_TEXT) == RECALL_REPLY
    memory_call = ros_memory.provider.calls[-1]
    assert len(memory_call['memories']) == 1
    assert memory_call['memories'][0].metadata['source']['text'] == FACT_TEXT

    assert ros_memory.say('강아지 이름 기억 삭제해줘') == '요청한 기억을 삭제했어요.'
    assert ros_memory.inspect_store()[1] == []
    assert ros_memory.say(RECALL_TEXT) == EMPTY_REPLY
    after_delete = ros_memory.provider.calls[-1]
    assert after_delete['memories'] == []
    assert all(
        '초코' not in turn.user_content + turn.assistant_content
        for turn in after_delete['history']
    )
    assert len(ros_memory.sent_ids) == len(set(ros_memory.sent_ids)) == 7


def test_ros_does_not_publish_answer_deleted_after_queue_drain(ros_memory):
    """A concurrent SQLite deletion replaces stale text before DDS publish."""
    ros_memory.enable_and_remember()
    record = ros_memory.inspect_store()[1][0]
    drain = ros_memory.agent.dialogue.drain
    deleted = []

    def delete_after_drain():
        replies = drain()
        if replies and not deleted:
            assert replies[0]['text'] == RECALL_REPLY
            store = SQLiteMemoryStore(ros_memory.path)
            try:
                assert store.delete(SPEAKER, record.id)
                deleted.append(record.id)
            finally:
                store.close()
        return replies

    ros_memory.agent.dialogue.drain = delete_after_drain
    previous_count = len(ros_memory.received)
    assert ros_memory.say(RECALL_TEXT) == MEMORY_CHANGED_RESPONSE
    assert deleted == [record.id]
    assert ros_memory.received[previous_count:] == [MEMORY_CHANGED_RESPONSE]
    assert ros_memory.inspect_store()[1] == []


def test_ros_restart_recalls_and_deletes_memory(ros_memory):
    """Restart the real Agent Node while retaining user and SQLite files."""
    ros_memory.enable_and_remember()
    saved = ros_memory.inspect_store()[1][0]
    ros_memory.restart()

    assert ros_memory.inspect_store()[0]['enabled'] is True
    assert ros_memory.say(RECALL_TEXT) == RECALL_REPLY
    after = ros_memory.provider.calls[-1]
    assert after['history'] == []
    assert [m.id for m in after['memories']] == [saved.id]
    assert ros_memory.say('강아지 이름 기억 삭제해줘') == '요청한 기억을 삭제했어요.'

    ros_memory.restart()
    assert ros_memory.inspect_store()[1] == []
    assert ros_memory.say(RECALL_TEXT) == EMPTY_REPLY
    assert ros_memory.provider.calls[-1]['history'] == []
    assert ros_memory.provider.calls[-1]['memories'] == []


def test_stt_pipeline_final_text_reaches_agent_and_tts(ros_memory):
    """Connect real capture policy and two ROS Topics with fixture vendors."""
    pipeline_module = pytest.importorskip('malbut_stt.pipeline')
    audio_module = pytest.importorskip('malbut_stt.audio')
    text = '  안녕\n'
    sent = []
    calls = []
    wake_calls = []
    phases = []
    stopped = []
    recorders = []
    recordings = iter([
        [[7] * 320] + [[0] * 320] * 20,
        [[1] * 320] + [[0] * 320] * 50,
    ])

    class Recorder:
        sample_rate = 16000
        active = False
        closed = False

        def __init__(self, frames):
            self.frames = iter(frames)

        def start(self):
            self.active = True

        def read(self):
            return next(self.frames)

        def stop(self):
            self.active = False

        def delete(self):
            self.closed = True

    def recorder_factory():
        assert not recorders or recorders[-1].closed
        recorder = Recorder(next(recordings))
        recorders.append(recorder)
        return recorder

    def recognize_wake(pcm, sample_rate):
        assert len(recorders) == 1
        assert recorders[-1].closed and not recorders[-1].active
        assert phases == ['waiting_for_wake', 'recognizing_wake']
        assert pcm == b'\x07\x00' * 320 + bytes(640 * 20)
        assert sample_rate == 16000
        wake_calls.append(pcm)
        return '제이크야'

    def transcribe(pcm, sample_rate):
        assert len(recorders) == 2
        assert recorders[-1].closed and not recorders[-1].active
        assert phases == [
            'waiting_for_wake', 'recognizing_wake', 'wake_detected',
            'listening', 'transcribing',
        ]
        assert pcm == b'\x01\x00' * 320 + bytes(640 * 50)
        assert sample_rate == 16000
        calls.append(pcm)
        return text

    def publish(utterance_id, original):
        assert str(UUID(utterance_id)) == utterance_id
        sent.append((utterance_id, original))
        ros_memory.publisher.publish(SpeechTranscript(
            utterance_id=utterance_id, text=original,
        ))

    def report(event):
        phases.append(event)
        if event.startswith('published:'):
            stopped.append(True)

    pipeline_module.SpeechPipeline(
        recorder_factory=recorder_factory,
        wake=SimpleNamespace(transcribe=recognize_wake),
        is_speech=lambda frame, _: frame[:2] in (b'\x07\x00', b'\x01\x00'),
        transcriber=SimpleNamespace(transcribe=transcribe),
        publish=publish, should_stop=lambda: bool(stopped), report=report,
        settings=audio_module.CaptureSettings(),
    ).run()

    ros_memory.spin_until(lambda: len(ros_memory.received) == 1)
    assert len(wake_calls) == len(calls) == len(sent) == 1
    assert len(recorders) == 2
    assert all(recorder.closed and not recorder.active for recorder in recorders)
    assert sent[0][1] == text
    assert ros_memory.events == [{
        'utterance_id': sent[0][0], 'text': text, 'status': 'received',
    }]
    # AgentRequest trims boundary whitespace after original ROS receipt.
    assert ros_memory.provider.calls[-1]['text'] == '안녕'
    assert ros_memory.received == ['말씀을 들었어요.']
    assert ros_memory.agent.missions._requests == {}
    assert ros_memory.inspect_store()[1] == []

    ros_memory.publisher.publish(SpeechTranscript(
        utterance_id=sent[0][0], text=text,
    ))
    ros_memory.spin_until(lambda: any(
        event.get('status') == 'duplicate' for event in ros_memory.events
    ))
    assert len(ros_memory.provider.calls) == 1
    assert ros_memory.received == ['말씀을 들었어요.']
