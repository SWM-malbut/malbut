"""Exercise asynchronous Service callbacks without a ROS installation."""

import sys
from types import SimpleNamespace

import pytest

from malbut_agent_server import ros_communication
from malbut_agent_server.speech_dialogue import validate_interruption_input


class Response:
    ADDRESSED = 'addressed'
    NOT_ADDRESSED = 'not_addressed'
    UNKNOWN = 'unknown'


class SpeechRequest(SimpleNamespace):
    DIALOGUE = 0
    NOTIFICATION = 1


class Future:
    def __init__(self, *, executor=None):
        self._done = False
        self._result = None

    def __await__(self):
        if not self._done:
            yield self
        assert self._done
        return self._result

    def done(self):
        return self._done

    def set_result(self, result):
        self._result = result
        self._done = True


class Invocation:
    def __init__(self, node, message):
        self.coroutine = node._classify_addressee(message, Response())
        self.response = None
        self.step()

    def step(self):
        try:
            self.future = self.coroutine.send(None)
        except StopIteration as result:
            self.response = result.value
        return self.response


@pytest.fixture
def node(monkeypatch, tmp_path, request):
    messages, subscriptions, services = {}, {}, {}

    class Node:
        def __init__(self, _name):
            self.context = SimpleNamespace(ok=lambda: True)
            self.executor = None
            self.default_callback_group = object()

        def create_publisher(self, message_type, topic, qos):
            messages[topic] = []
            return SimpleNamespace(publish=messages[topic].append)

        def create_subscription(self, message_type, topic, callback, qos):
            subscriptions[topic] = (callback, qos)

        def create_service(self, service_type, name, callback, *, callback_group):
            services[name] = (service_type, callback, callback_group)

        def create_timer(self, *_args):
            return None

        def get_logger(self):
            return SimpleNamespace(info=lambda *_: None, error=lambda *_: None,
                                   warning=lambda *_: None)

        def destroy_node(self):
            return True

    class Worker:
        startup_error = None
        ready = getattr(request, 'param', True)
        accept = True

        def __init__(self, *_args):
            self.requests = []
            self.results = []

        def submit_interruption(self, uid, pid, text):
            validate_interruption_input(uid, pid, text)
            self.requests.append((uid, pid, text))
            return self.accept and self.startup_error is None

        def drain(self):
            result, self.results = self.results, []
            return result

        def publish_reply(self, reply, publish):
            assert reply['kind'] != 'addressee'
            return reply if publish(reply['text']) else None

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, 'rclpy.node', SimpleNamespace(Node=Node))
    monkeypatch.setitem(sys.modules, 'rclpy.callback_groups', SimpleNamespace(
        ReentrantCallbackGroup=type('ReentrantCallbackGroup', (), {}),
    ))
    monkeypatch.setitem(sys.modules, 'rclpy.task', SimpleNamespace(Future=Future))
    monkeypatch.setitem(sys.modules, 'rclpy.qos', SimpleNamespace(
        DurabilityPolicy=SimpleNamespace(VOLATILE='volatile'),
        HistoryPolicy=SimpleNamespace(KEEP_LAST='keep-last'),
        ReliabilityPolicy=SimpleNamespace(RELIABLE='reliable'),
        QoSProfile=lambda **kwargs: kwargs,
    ))
    monkeypatch.setitem(sys.modules, 'malbut_interfaces.msg', SimpleNamespace(
        SpeechRequest=SpeechRequest, SpeechTranscript=SimpleNamespace,
    ))
    monkeypatch.setitem(sys.modules, 'malbut_interfaces.srv', SimpleNamespace(
        ClassifySpeechAddressee=SimpleNamespace(Response=Response),
    ))
    monkeypatch.setattr(ros_communication, 'DialogueWorker', Worker)
    monkeypatch.setattr('malbut_agent_server.manager_client.ManagerClient',
                        lambda *_args, **_kwargs: SimpleNamespace(close=lambda: None))
    instance = ros_communication.create_communication_node(
        speech_db_path=str(tmp_path / 'receipts.sqlite3'),
    )
    instance.sent = messages
    instance.subscriptions = subscriptions
    instance.services = services
    yield instance
    instance.destroy_node()


def request(uid='uid', pid='pid', text='  원문\n'):
    return SimpleNamespace(utterance_id=uid, playback_id=pid, text=text)


def result(uid='uid', pid='pid', decision='addressed'):
    return {'kind': 'addressee', 'utterance_id': uid,
            'playback_id': pid, 'decision': decision}


@pytest.mark.parametrize('node', [False], indirect=True)
def test_speech_endpoints_wait_for_worker_initialization(node):
    """A peer probe must not admit STT while the dialogue DB is still opening."""
    assert node.subscriptions == node.services == {}
    node._drain_dialogue()
    assert node.subscriptions == node.services == {}
    node.dialogue.ready = True
    node._drain_dialogue()
    assert set(node.subscriptions) == {ros_communication.TRANSCRIPT_TOPIC}
    assert set(node.services) == {ros_communication.ADDRESSEE_SERVICE}


@pytest.mark.parametrize('node', [False], indirect=True)
def test_failed_initialization_never_advertises_speech_endpoints(node):
    """Failed startup remains fatal without momentarily passing discovery."""
    node.dialogue.startup_error = 'DatabaseError'
    with pytest.raises(RuntimeError, match='speech_dialogue_startup_failed'):
        node._drain_dialogue()
    assert node.subscriptions == node.services == {}


def test_service_yields_until_timer_drains_without_speech_or_receipt(node):
    service, callback, group = node.services[ros_communication.ADDRESSEE_SERVICE]
    assert service.Response is Response
    assert callback == node._classify_addressee
    assert group is not node.default_callback_group
    invocation = Invocation(node, request())
    assert invocation.response is None
    assert node.dialogue.requests == [('uid', 'pid', '  원문\n')]
    assert all(not messages for messages in node.sent.values())
    node.dialogue.results = [result()]
    node._drain_dialogue()
    assert vars(invocation.step()) == {'decision': Response.ADDRESSED}
    assert node.sent[ros_communication.RESPONSE_TOPIC] == []
    assert node._receipts.lookup('uid', '  원문\n') is None
    assert node._addressee_waiters == {}


def test_each_concurrent_duplicate_gets_the_shared_classification_result(node):
    first, second = Invocation(node, request()), Invocation(node, request())
    assert first.future is not second.future
    node.dialogue.results = [result(decision='not_addressed')]
    node._drain_dialogue()
    assert first.step().decision == second.step().decision == Response.NOT_ADDRESSED
    assert node._addressee_waiters == {}


def test_result_only_resolves_the_matching_request_ids(node):
    first = Invocation(node, request())
    second = Invocation(node, request(uid='other', pid='other-playback'))
    node.dialogue.results = [result(pid='stale'), result()]
    node._drain_dialogue()
    assert first.step().decision == Response.ADDRESSED
    assert not second.future.done()
    node.dialogue.results = [result('other', 'other-playback', 'unknown')]
    node._drain_dialogue()
    assert second.step().decision == Response.UNKNOWN


@pytest.mark.parametrize('reason', ['capacity', 'startup', 'blank', 'oversize'])
def test_rejected_interruption_returns_unknown_without_speaking(node, reason):
    message = request()
    if reason == 'capacity':
        node.dialogue.accept = False
    elif reason == 'startup':
        node.dialogue.startup_error = 'RuntimeError'
    elif reason == 'blank':
        message.text = ' '
    else:
        message.text = 'x' * 2001
    invocation = Invocation(node, message)
    assert vars(invocation.response) == {'decision': Response.UNKNOWN}
    assert node.sent[ros_communication.RESPONSE_TOPIC] == []
    assert node._receipts.lookup('uid', '원문') is None
    assert node._addressee_waiters == {}


def test_duplicate_waiters_are_bounded(node, monkeypatch):
    monkeypatch.setattr(ros_communication, 'MAX_PENDING_ADDRESSEE_REQUESTS', 1)
    first = Invocation(node, request())
    second = Invocation(node, request())
    assert second.response.decision == Response.UNKNOWN
    assert len(node.dialogue.requests) == 1
    node.dialogue.results = [result()]
    node._drain_dialogue()
    assert first.step().decision == Response.ADDRESSED


def test_invalid_worker_decision_uses_generated_unknown_constant(node, monkeypatch):
    monkeypatch.setattr(Response, 'UNKNOWN', 'interface-unknown')
    invocation = Invocation(node, request())
    node.dialogue.results = [result(decision='invalid')]
    node._drain_dialogue()
    assert invocation.step().decision == Response.UNKNOWN


def test_normal_dialogue_answers_keep_the_existing_tts_path(node):
    original = '  일반 대화 답변\n다음 문장도 원문대로.  '
    node.dialogue.results = [
        result(),
        {'kind': 'answer', 'utterance_id': 'normal',
         'conversation_id': 'conversation', 'text': original},
    ]
    node._drain_dialogue()
    messages = node.sent[ros_communication.RESPONSE_TOPIC]
    assert [(message.text, message.request_type) for message in messages] == [
        (original, SpeechRequest.DIALOGUE),
    ]
    assert list(node.sent) == [ros_communication.RESPONSE_TOPIC]


def test_failed_dialogue_startup_is_fatal_to_executor(node):
    """A permanently unusable worker must not leave advertised services alive."""
    node.dialogue.startup_error = 'DatabaseError'
    with pytest.raises(RuntimeError, match='speech_dialogue_startup_failed'):
        node._drain_dialogue()
    assert all(not messages for messages in node.sent.values())


def test_mission_announcements_are_notification_requests(node):
    node._mission_event({
        'kind': 'succeeded', 'request_id': 'patrol-1',
        'capability_id': 'patrol',
    })
    messages = node.sent[ros_communication.RESPONSE_TOPIC]
    assert [(message.text, message.request_type) for message in messages] == [
        ('순찰 요청: Manager가 실행 요청을 성공 상태로 종료했다고 알려왔어요.',
         SpeechRequest.NOTIFICATION),
    ]


def test_direct_say_preserves_text_and_defaults_to_dialogue(node):
    original = '  직접 발행한 답변.\n원문의 줄바꿈도 유지.  '
    assert node.say(original)
    assert not node.say(' \n ')
    messages = node.sent[ros_communication.RESPONSE_TOPIC]
    assert [(message.text, message.request_type) for message in messages] == [
        (original, SpeechRequest.DIALOGUE),
    ]


def test_shutdown_resolves_pending_and_late_requests_as_unknown(node):
    pending = Invocation(node, request())
    node.destroy_node()
    assert pending.step().decision == Response.UNKNOWN
    late = Invocation(node, request(uid='late'))
    assert late.response.decision == Response.UNKNOWN
    assert node.dialogue.requests == [('uid', 'pid', '  원문\n')]
    assert node._addressee_waiters == {}
    assert all(not messages for messages in node.sent.values())


def test_shutdown_overrides_a_result_before_the_service_callback_returns(node):
    pending = Invocation(node, request())
    node.dialogue.results = [result()]
    node._drain_dialogue()
    assert pending.future.done()
    node.begin_shutdown()
    assert pending.step().decision == Response.UNKNOWN
