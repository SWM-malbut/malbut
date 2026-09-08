"""Optional ROS communication tests without models, hardware, or audio."""

import sqlite3
from threading import Event, Thread
import time
from types import SimpleNamespace

import pytest


rclpy = pytest.importorskip('rclpy', reason='ROS 2 is not installed')
yaml = pytest.importorskip('yaml')

# Dependent imports follow the optional ROS gate.

from action_msgs.msg import GoalStatus  # noqa: E402
from malbut_interfaces.action import ExecuteMission, FollowPerson  # noqa: E402
from malbut_interfaces.msg import SpeechTranscript  # noqa: E402
from rclpy.action import (  # noqa: E402
    ActionClient, ActionServer, CancelResponse, GoalResponse,
)
from rclpy.callback_groups import ReentrantCallbackGroup  # noqa: E402
from rclpy.executors import (  # noqa: E402
    MultiThreadedExecutor, SingleThreadedExecutor,
)
from rclpy.node import Node  # noqa: E402

from malbut_system_manager.system_manager_node import (  # noqa: E402
    SystemManagerNode,
)
from malbut_tts import receiver as tts_receiver  # noqa: E402

from malbut_agent_server.ros_communication import (  # noqa: E402
    create_communication_node,
)
from malbut_agent_server.speech_receipts import (  # noqa: E402
    SpeechReceiptStore,
)
from malbut_agent_server.config import Settings  # noqa: E402
from malbut_agent_server import factory as runtime_factory  # noqa: E402
from malbut_agent_server.providers.base import (  # noqa: E402
    AgentProvider, ProviderError,
)
from malbut_agent_server.schemas import (  # noqa: E402
    AgentDecision, ProviderResult,
)


TIMEOUT_S = 8.0


def _wait_until(predicate, timeout=TIMEOUT_S):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError('ROS evidence did not arrive before the deadline')


class _FollowServer(Node):
    """Test-only implementation of the real downstream ROS Action type."""

    def __init__(self):
        super().__init__('communication_test_follow_server')
        self.goals = {}
        self.finish = {}
        self.cancel_seen = {}
        self.allow_cancel = Event()
        self.server = ActionServer(
            self, FollowPerson, '/communication_test/follow_person',
            execute_callback=self.execute,
            goal_callback=lambda _request: GoalResponse.ACCEPT,
            cancel_callback=lambda _handle: CancelResponse.ACCEPT,
            callback_group=ReentrantCallbackGroup(),
        )

    def execute(self, handle):
        """Let each test choose success or delay terminal cancellation."""
        target = handle.request.target_person_id
        self.finish.setdefault(target, Event())
        self.cancel_seen.setdefault(target, Event())
        self.goals.setdefault(target, []).append(handle)
        deadline = time.monotonic() + 2 * TIMEOUT_S
        while time.monotonic() < deadline:
            if handle.is_cancel_requested:
                self.cancel_seen[target].set()
                if self.allow_cancel.wait(TIMEOUT_S):
                    handle.canceled()
                    return FollowPerson.Result(
                        success=False, final_state='STOPPED',
                        message='test action canceled',
                    )
                break
            if self.finish[target].is_set():
                handle.succeed()
                return FollowPerson.Result(
                    success=True, final_state='STOPPED',
                    message='test action finished for ' + target,
                )
            handle.publish_feedback(FollowPerson.Feedback(
                state='TRACKING', target_visible=True,
            ))
            time.sleep(0.05)
        handle.abort()
        return FollowPerson.Result(message='test server deadline expired')

    def release(self):
        """Unblock all callbacks during bounded fixture cleanup."""
        self.allow_cancel.set()
        for event in self.finish.values():
            event.set()

    def destroy_node(self):
        self.server.destroy()
        return super().destroy_node()


def _write_test_manifest(directory):
    """Register only a test server; never edit the installed robot catalog."""
    directory.mkdir()
    fields = {
        'target_mode': {'type': 'uint8', 'description': 'Target selection'},
        'target_person_id': {'type': 'string', 'description': 'Test target'},
        'desired_distance_m': {'type': 'float32', 'description': 'Distance'},
    }
    manifest = {
        'schema_version': 1,
        'capability': {
            'id': 'follow_person', 'title': 'Test follow',
            'description': 'Test-only Action communication without movement',
        },
        'command': {
            'kind': 'ACTION', 'name': '/communication_test/follow_person',
            'type': 'malbut_interfaces/action/FollowPerson',
        },
        'input': {'fields': fields},
        'execution': {
            'mode': 'BACKGROUND', 'priority': 'NORMAL', 'resources': [],
        },
    }
    (directory / 'follow_person.yaml').write_text(
        yaml.safe_dump(manifest), encoding='utf-8',
    )


def _arguments(target):
    return {
        'target_mode': FollowPerson.Goal.REGISTERED_PERSON,
        'target_person_id': target, 'desired_distance_m': 1.0,
    }


class _DialogueProvider(AgentProvider):
    """Fixed inference seam; real orchestration still builds turn context."""

    def __init__(self, *, blocked=False, fail_first=False):
        self.started = Event()
        self.release = Event()
        self.calls = []
        self.histories = []
        self.fail_first = fail_first
        if not blocked:
            self.release.set()

    def complete(self, request, memories, conversation_turns, tools,
                 conversation_summary=None):
        """Use supplied prior turns to answer the second name question."""
        del memories, conversation_summary
        assert not tools, 'Dialogue must not expose executable tools'
        call_number = len(self.calls)
        self.calls.append(request)
        self.histories.append(list(conversation_turns))
        self.started.set()
        if not self.release.wait(TIMEOUT_S):
            raise ProviderError('test provider wait expired')
        if self.fail_first and call_number == 0:
            raise ProviderError('test-only inference failure')
        if request.utterance == '내 이름은 봄이야':
            reply = '봄이라고 소개해 주셨네요.'
        elif request.utterance == '내 이름이 뭐라고 했지?':
            named = any(turn.user_content == '내 이름은 봄이야'
                        for turn in conversation_turns)
            reply = '이름은 봄이에요.' if named else '이름을 알 수 없어요.'
        else:
            reply = '대화 연결 확인 응답'
        return ProviderResult(
            decision=AgentDecision(type='message', message=reply),
            provider='ros-dialogue-fixture', model='fixed', latency_ms=0,
        )


@pytest.fixture
def communication(tmp_path, monkeypatch):
    """Connect production nodes on a loopback-only, isolated ROS domain."""
    monkeypatch.setenv('ROS_DOMAIN_ID', '193')
    monkeypatch.setenv('ROS_LOCALHOST_ONLY', '1')
    rclpy.init()
    nodes = []
    manager = None
    follow = None
    executor = MultiThreadedExecutor(num_threads=8)
    thread = Thread(target=executor.spin, daemon=True)
    agent_thread = None
    agent_stop = Event()
    received = []
    events = []
    receipts = []
    providers = []
    original_receive = tts_receiver.receive_text
    original_receipt = SpeechReceiptStore.receive

    def receive(text, logger):
        accepted = original_receive(text, logger)
        if accepted:
            received.append(text)
        return accepted

    monkeypatch.setattr(tts_receiver, 'receive_text', receive)

    def receipt(store, utterance_id, text):
        outcome = original_receipt(store, utterance_id, text)
        receipts.append(outcome)
        return outcome

    monkeypatch.setattr(SpeechReceiptStore, 'receive', receipt)

    def create(*, with_manager=True, provider=None):
        nonlocal manager, follow, agent_thread
        assert not nodes, 'Each test owns one isolated communication graph'
        settings = Settings(
            database_path=str(tmp_path / 'dialogue.sqlite3'),
            user_id='speech-test-user',
        )
        dialogue_factory = None
        if provider is not None:
            providers.append(provider)
            monkeypatch.setattr(runtime_factory, 'build_provider',
                                lambda _settings, **_kwargs: provider)

            def create_runtime():
                return runtime_factory.build_orchestrator(
                    settings, http_server=False,
                )

            dialogue_factory = create_runtime

        if with_manager:
            directory = tmp_path / 'test-manifests'
            _write_test_manifest(directory)
            follow = _FollowServer()
            manager = SystemManagerNode(manifest_directory=str(directory))
            nodes.extend([follow, manager])
        agent_ready = Event()
        agent_state = {}

        def run_agent():
            # Match the production CLI: receipt DB creation, callbacks and
            # closure share one thread; only ROS communication crosses it.
            agent_executor = SingleThreadedExecutor()
            agent = None
            try:
                agent = create_communication_node(
                    speech_db_path=str(tmp_path / 'speech.sqlite3'),
                    on_event=events.append, goal_response_timeout_s=1.0,
                    dialogue_settings=settings,
                    dialogue_factory=dialogue_factory,
                )
                agent_executor.add_node(agent)
                agent_state['node'] = agent
                agent_ready.set()
                while not agent_stop.is_set():
                    agent_executor.spin_once(timeout_sec=0.05)
            except Exception as error:
                agent_state['error'] = error
                agent_ready.set()
            finally:
                agent_executor.shutdown(timeout_sec=1.0)
                if agent is not None:
                    agent.destroy_node()

        agent_thread = Thread(target=run_agent, daemon=True)
        agent_thread.start()
        _wait_until(agent_ready.is_set)
        if 'error' in agent_state:
            raise agent_state['error']
        agent = agent_state['node']
        closing = Event()
        original_close = agent.dialogue.close

        def close_dialogue():
            closing.set()
            return original_close()

        monkeypatch.setattr(agent.dialogue, 'close', close_dialogue)
        tts = tts_receiver.create_receiver_node()
        sender = Node('communication_test_sender')
        nodes.extend([tts, sender])
        for node in nodes:
            executor.add_node(node)
        thread.start()
        _wait_until(lambda: agent.count_subscribers(
            '/malbut/speech/response',
        ) == 1)
        for get_endpoints in (
            agent.get_publishers_info_by_topic,
            agent.get_subscriptions_info_by_topic,
        ):
            assert {endpoint.topic_type for endpoint in get_endpoints(
                '/malbut/speech/response',
            )} == {'malbut_interfaces/msg/SpeechRequest'}
        probe = ActionClient(sender, ExecuteMission, '/malbut/mission/execute')
        try:
            assert probe.wait_for_server(timeout_sec=1.0) is with_manager
        finally:
            probe.destroy()
        return SimpleNamespace(
            agent=agent, follow=follow, events=events,
            speech=received, sender=sender, db=tmp_path / 'speech.sqlite3',
            receipts=receipts,
            stop_agent=agent_stop.set, agent_thread=agent_thread,
            dialogue_closing=closing,
        )

    try:
        yield create
    finally:
        for provider in providers:
            provider.release.set()
        if manager is not None:
            manager.begin_shutdown()
        if follow is not None:
            follow.release()
        if manager is not None:
            try:
                _wait_until(lambda: manager.public_context_count == 0, 3.0)
            except AssertionError:
                manager.force_shutdown()
        agent_stop.set()
        if agent_thread is not None:
            agent_thread.join(timeout=3.0)
        executor.shutdown(timeout_sec=3.0)
        if thread.ident is not None:
            thread.join(timeout=3.0)
        for node in reversed(nodes):
            node.destroy_node()
        executor.shutdown(timeout_sec=1.0)
        rclpy.shutdown()


def _events(graph, request_id, kind):
    return [event for event in graph.events
            if event['request_id'] == request_id and event['kind'] == kind]


def test_follow_feedback_and_result_reach_real_tts_receiver(communication):
    """Exercise Agent, Manager and downstream Action over actual ROS DDS."""
    graph = communication()
    request_id = graph.agent.missions.submit(
        'follow_person', _arguments('one'),
    )
    _wait_until(lambda: 'one' in graph.follow.goals)
    _wait_until(lambda: any(
        'target_visible: true' in (event.get('feedback_yaml') or '')
        for event in _events(graph, request_id, 'progress')
    ))
    before = graph.agent.missions.snapshot(request_id)
    assert before['accepted'] is True
    assert not before['terminal']
    assert before['goal_id'] == before['mission_id']

    graph.follow.finish['one'].set()
    _wait_until(lambda: graph.agent.missions.snapshot(request_id)['terminal'])
    final = graph.agent.missions.snapshot(request_id)
    assert final['ros_status'] == GoalStatus.STATUS_SUCCEEDED
    assert yaml.safe_load(final['result_yaml'])['message'].endswith('one')
    assert len(graph.follow.goals['one']) == 1
    _wait_until(lambda: any('성공' in text for text in graph.speech))


def test_invalid_capability_is_accepted_then_aborted_without_execution(
    communication,
):
    """Action admission alone never becomes an execution-success response."""
    graph = communication()
    request_id = graph.agent.missions.submit('not_registered', {})
    _wait_until(lambda: graph.agent.missions.snapshot(request_id)['terminal'])
    final = graph.agent.missions.snapshot(request_id)
    assert _events(graph, request_id, 'accepted')
    assert final['ros_status'] == GoalStatus.STATUS_ABORTED
    assert 'Unknown capability_id' in final['reason']
    assert not graph.follow.goals
    _wait_until(lambda: any('실패' in text for text in graph.speech))
    assert not any('성공' in text for text in graph.speech)


def test_cancel_receipt_precedes_final_canceled_result(communication):
    """Hold downstream cancellation to expose the two distinct stages."""
    graph = communication()
    request_id = graph.agent.missions.submit(
        'follow_person', _arguments('hold'),
    )
    _wait_until(lambda: 'hold' in graph.follow.goals)
    graph.agent.missions.cancel(request_id)
    _wait_until(lambda: _events(graph, request_id, 'cancel_accepted'))
    _wait_until(lambda: graph.follow.cancel_seen['hold'].is_set())
    assert not graph.agent.missions.snapshot(request_id)['terminal']
    assert not _events(graph, request_id, 'canceled')
    _wait_until(lambda: any('취소 요청' in text for text in graph.speech))

    graph.follow.allow_cancel.set()
    _wait_until(lambda: graph.agent.missions.snapshot(request_id)['terminal'])
    final = graph.agent.missions.snapshot(request_id)
    assert final['ros_status'] == GoalStatus.STATUS_CANCELED
    _wait_until(lambda: any('취소 상태' in text for text in graph.speech))
    assert not any('로봇이 멈췄' in text for text in graph.speech)


def test_duplicate_requests_and_repeated_progress_do_not_resend_or_speak(
    communication,
):
    """Keep one Goal and one announcement for each observed state."""
    graph = communication()
    arguments = _arguments('duplicate')
    request_id = graph.agent.missions.submit(
        'follow_person', arguments, request_id='same-request',
    )
    _wait_until(lambda: len(_events(graph, request_id, 'progress')) >= 3)
    same_id = graph.agent.missions.submit(
        'follow_person', arguments, request_id='same-request',
    )
    assert same_id == request_id
    with pytest.raises(ValueError, match='different input'):
        graph.agent.missions.submit(
            'follow_person', _arguments('changed'), request_id='same-request',
        )
    assert len(graph.follow.goals['duplicate']) == 1
    _wait_until(lambda: any('실행 중인' in text for text in graph.speech))
    assert len([text for text in graph.speech if '실행 중인' in text]) == 1
    graph.follow.finish['duplicate'].set()
    _wait_until(lambda: graph.agent.missions.snapshot(request_id)['terminal'])
    graph.agent.missions.submit(
        'follow_person', arguments, request_id='same-request',
    )
    assert len(graph.follow.goals['duplicate']) == 1


def test_two_independent_requests_keep_their_own_results(communication):
    """The resource-free test capability may run twice without reassignment."""
    graph = communication()
    first = graph.agent.missions.submit('follow_person', _arguments('first'))
    second = graph.agent.missions.submit('follow_person', _arguments('second'))
    _wait_until(lambda: set(graph.follow.goals) == {'first', 'second'})
    graph.follow.finish['second'].set()
    _wait_until(lambda: graph.agent.missions.snapshot(second)['terminal'])
    assert not graph.agent.missions.snapshot(first)['terminal']
    graph.follow.finish['first'].set()
    _wait_until(lambda: graph.agent.missions.snapshot(first)['terminal'])
    snapshots = [graph.agent.missions.snapshot(item)
                 for item in (first, second)]
    assert snapshots[0]['goal_id'] != snapshots[1]['goal_id']
    for label, snapshot in zip(('first', 'second'), snapshots):
        assert snapshot['mission_id'] == snapshot['goal_id']
        result = yaml.safe_load(snapshot['result_yaml'])
        assert result['message'].endswith(label)


def test_unavailable_manager_does_not_claim_execution(communication):
    """A missing server produces a local unavailable result without a Goal."""
    graph = communication(with_manager=False)
    request_id = graph.agent.missions.submit(
        'follow_person', _arguments('none'),
    )
    final = graph.agent.missions.snapshot(request_id)
    assert final['state'] == 'UNAVAILABLE'
    assert final['accepted'] is None
    assert final['mission_id'] is None
    assert not _events(graph, request_id, 'submitted')
    _wait_until(lambda: bool(graph.speech))
    assert not any('성공' in text for text in graph.speech)


def _publish_transcript(graph, utterance_id, text, *, repeat=1):
    publisher = graph.sender.create_publisher(
        SpeechTranscript, '/malbut/speech/transcript', 10,
    )
    _wait_until(lambda: publisher.get_subscription_count() == 1)
    message = SpeechTranscript(utterance_id=utterance_id, text=text)
    for _ in range(repeat):
        publisher.publish(message)


def test_stt_duplicate_has_one_reply_and_next_turn_uses_real_history(
    communication,
):
    """Real orchestration builds context; only the model is a fixed seam."""
    provider = _DialogueProvider()
    graph = communication(provider=provider)
    _publish_transcript(graph, 'speech-once', '내 이름은 봄이야', repeat=2)

    def receipt_count():
        with sqlite3.connect(graph.db) as connection:
            return connection.execute(
                'SELECT count(*) FROM speech_receipts',
            ).fetchone()[0]

    _wait_until(lambda: graph.receipts == ['received', 'duplicate'])
    _wait_until(lambda: graph.speech == ['봄이라고 소개해 주셨네요.'])
    assert receipt_count() == 1
    assert len(provider.calls) == 1
    _publish_transcript(graph, 'speech-next', '내 이름이 뭐라고 했지?')
    _wait_until(lambda: graph.speech == [
        '봄이라고 소개해 주셨네요.', '이름은 봄이에요.',
    ])
    assert receipt_count() == 2
    assert len(provider.calls) == 2
    assert provider.histories[0] == []
    assert provider.histories[1][0].user_content == '내 이름은 봄이야'
    assert provider.histories[1][0].assistant_content == '봄이라고 소개해 주셨네요.'
    assert (provider.calls[0].conversation_id
            == provider.calls[1].conversation_id)
    assert provider.calls[0].turn_id != provider.calls[1].turn_id
    assert not graph.events
    assert not graph.follow.goals
    direct_text = ' 직접 입력한 응답.\n"말벗"입니다.\t'
    assert graph.agent.say(direct_text)
    _wait_until(lambda: graph.speech[-1] == direct_text)
    assert not graph.agent.say('  ')


def test_slow_dialogue_does_not_block_manager_feedback_or_cancel(
    communication,
):
    """ROS control reaches final cancellation while inference is blocked."""
    provider = _DialogueProvider(blocked=True)
    graph = communication(provider=provider)
    request_id = graph.agent.missions.submit(
        'follow_person', _arguments('during-dialogue'),
    )
    _wait_until(lambda: 'during-dialogue' in graph.follow.goals)
    _publish_transcript(graph, 'slow-speech', '안녕')
    _wait_until(provider.started.is_set)
    progress_count = len(_events(graph, request_id, 'progress'))
    _wait_until(lambda: len(_events(graph, request_id, 'progress'))
                > progress_count)
    graph.agent.missions.cancel(request_id)
    _wait_until(lambda: _events(graph, request_id, 'cancel_accepted'))
    _wait_until(lambda: graph.follow.cancel_seen['during-dialogue'].is_set())
    assert not graph.agent.missions.snapshot(request_id)['terminal']
    graph.follow.allow_cancel.set()
    _wait_until(lambda: graph.agent.missions.snapshot(request_id)['terminal'])
    assert (graph.agent.missions.snapshot(request_id)['ros_status']
            == GoalStatus.STATUS_CANCELED)
    assert not provider.release.is_set()
    assert '대화 연결 확인 응답' not in graph.speech
    provider.release.set()
    _wait_until(lambda: '대화 연결 확인 응답' in graph.speech)


def test_dialogue_failure_is_reported_and_next_turn_recovers(communication):
    """A provider failure leaves the real conversation runtime reusable."""
    provider = _DialogueProvider(fail_first=True)
    graph = communication(provider=provider)
    _publish_transcript(graph, 'failed-speech', '첫 번째 대화')
    _wait_until(lambda: len(graph.speech) == 1)
    assert graph.speech == ['답변을 만들지 못했어요. 다시 말씀해 주세요.']
    _publish_transcript(graph, 'recovered-speech', '다시 대화하자')
    _wait_until(lambda: graph.speech[-1] == '대화 연결 확인 응답')
    assert len(provider.calls) == 2
    assert provider.histories[1] == []
    assert not graph.events
    assert not graph.follow.goals


def test_busy_dialogue_keeps_new_receipt_available_for_retry(communication):
    """A full worker preserves dedup and does not consume a refused ID."""
    provider = _DialogueProvider(blocked=True)
    graph = communication(provider=provider)
    _publish_transcript(graph, 'queued-0', '대화 0')
    _wait_until(provider.started.is_set)
    for index in range(1, 10):
        _publish_transcript(graph, f'queued-{index}', f'대화 {index}')
    _wait_until(lambda: graph.receipts == ['received'] * 10)
    _publish_transcript(graph, 'queued-10', '대화 10')
    _wait_until(lambda: graph.speech == [
        '앞선 대화를 처리하고 있어요. 잠시 뒤 다시 말씀해 주세요.',
    ])
    _publish_transcript(graph, 'queued-0', '대화 0')
    _wait_until(lambda: graph.receipts == ['received'] * 10 + ['duplicate'])
    with sqlite3.connect(graph.db) as connection:
        stored = connection.execute(
            'SELECT utterance_id FROM speech_receipts',
        ).fetchall()
    assert len(stored) == 10
    assert ('queued-10',) not in stored

    provider.release.set()
    _wait_until(lambda: graph.speech.count('대화 연결 확인 응답') == 10)
    _publish_transcript(graph, 'queued-10', '대화 10')
    _wait_until(lambda: graph.speech.count('대화 연결 확인 응답') == 11)
    assert len(provider.calls) == 11
    assert graph.receipts == ['received'] * 10 + ['duplicate', 'received']
    assert not graph.events
    assert not graph.follow.goals


def test_shutdown_discards_late_dialogue_result_on_real_topic(communication):
    """Closing waits for inference but does not publish its late answer."""
    provider = _DialogueProvider(blocked=True)
    graph = communication(provider=provider)
    _publish_transcript(graph, 'closing-speech', '안녕')
    _wait_until(provider.started.is_set)
    graph.stop_agent()
    _wait_until(graph.dialogue_closing.is_set)
    provider.release.set()
    _wait_until(lambda: not graph.agent_thread.is_alive())
    # The TTS executor remains alive after the Agent and drains pending DDS.
    time.sleep(0.15)
    assert not graph.speech
    assert not graph.events
    assert not graph.follow.goals
