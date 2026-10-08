"""Exercise asynchronous Service callbacks without a ROS installation."""

import sqlite3
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


class SpeechInputStatus(SimpleNamespace):
    STARTED = 'started'
    FAILED = 'failed'


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
        suspended = False
        has_pending = False

        def __init__(self, *_args):
            self.requests = []
            self.results = []
            self.cancelled = []

        def cancel_request(self, uid):
            self.cancelled.append(uid)
            return True

        def submit_interruption(self, uid, pid, text):
            validate_interruption_input(uid, pid, text)
            self.requests.append((uid, pid, text))
            return self.accept and self.startup_error is None

        def has_capacity(self):
            return self.ready and self.accept and not self.suspended

        def submit(self, uid, text):
            self.requests.append((uid, text))
            return self.has_capacity()

        def suspend(self):
            self.suspended = True

        def resume(self):
            self.suspended = False

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
        SpeechInputStatus=SpeechInputStatus,
        SpeechRequestStatus=SimpleNamespace,
    ))
    monkeypatch.setitem(sys.modules, 'malbut_interfaces.srv', SimpleNamespace(
        ClassifySpeechAddressee=SimpleNamespace(Response=Response),
        CancelSpeechRequest=SimpleNamespace,
    ))
    monkeypatch.setattr(ros_communication, 'DialogueWorker', Worker)
    monkeypatch.setattr(ros_communication, 'SituationActionServer',
                        lambda *_args, **_kwargs: SimpleNamespace(
                            active=False, close=lambda: None, destroy=lambda: None,
                        ))
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


def test_request_receipt_follows_admission_without_extra_speech(node):
    node._receive_speech(request(uid='turn', text='안녕'))
    statuses = node.sent['/malbut/speech/request_status']
    assert [(s.request_id, s.state, s.reason) for s in statuses] == [
        ('turn', 'accepted', '')]
    assert node.dialogue.requests == [('turn', '안녕')]
    assert node.sent[ros_communication.RESPONSE_TOPIC] == []


def test_request_rejection_is_silent_and_does_not_consume_receipt(node):
    node.dialogue.accept = False
    node._receive_speech(request(uid='busy', text='안녕'))
    status = node.sent['/malbut/speech/request_status'][-1]
    assert (status.request_id, status.state, status.reason) == ('busy', 'rejected', 'busy')
    assert node._receipts.lookup('busy', '안녕') is None
    assert node.sent[ros_communication.RESPONSE_TOPIC] == []


@pytest.mark.parametrize('node', [False], indirect=True)
def test_cancel_reservation_is_available_before_agent_ready_and_rejects_late_transcript(node):
    _, cancel, _ = node.services['/malbut/speech/cancel_request']
    assert cancel(SimpleNamespace(request_id='lost'), SimpleNamespace()).accepted
    node.dialogue.ready = True
    node._drain_dialogue()
    node._receive_speech(request(uid='lost', text='late transcript'))
    assert node.dialogue.requests == []
    assert node._receipts.lookup('lost', 'late transcript') is None
    assert node.sent[ros_communication.RESPONSE_TOPIC] == []
    assert node.sent['/malbut/speech/request_status'][-1].state == 'cancelled'


def result(uid='uid', pid='pid', decision='addressed'):
    return {'kind': 'addressee', 'utterance_id': uid,
            'playback_id': pid, 'decision': decision}


@pytest.mark.parametrize('node', [False], indirect=True)
def test_volatile_startup_loss_recovers_and_cancellation_fences_late_agent_output(node):
    """Join real STT deadlines to Agent admission while its subscription is absent."""
    from malbut_stt.dialogue_pipeline import DialoguePipeline

    now = [0.0]
    events = []

    def publish(uid, text):
        subscriber = node.subscriptions.get(ros_communication.TRANSCRIPT_TOPIC)
        if subscriber is not None:
            subscriber[0](request(uid=uid, text=text))

    def cancel(uid):
        assert node._cancel_request(
            SimpleNamespace(request_id=uid), SimpleNamespace()).accepted

    pipeline = DialoguePipeline(
        recorder_factory=None, wake=None, transcriber=SimpleNamespace(),
        is_speech=lambda *_: False, publish_transcript=publish,
        publish_control=lambda *_: None, publish_interruption=lambda *_: None,
        report=events.append, cancel_request=cancel, clock=lambda: now[0])
    # VOLATILE DDS does not retain this publication for the later subscriber.
    pipeline._publish_transcript('lost', 'first')
    assert node.dialogue.requests == []
    assert pipeline._reply_request_id == 'lost'
    now[0] = 5.0
    pipeline.poll()
    assert pipeline._reply_request_id is None
    assert node.dialogue.cancelled == ['lost']
    assert any('receipt_timeout' in event for event in events)

    node.dialogue.ready = True
    node._drain_dialogue()
    publish('lost', 'late')
    assert node.dialogue.requests == []
    # This also models a result already drained by an earlier executor callback.
    node.dialogue.results = [dict(kind='answer', utterance_id='lost', text='obsolete')]
    node._drain_dialogue()
    assert node.sent[ros_communication.RESPONSE_TOPIC] == []

    pipeline._publish_transcript('new', 'next')
    status = node.sent[ros_communication.REQUEST_STATUS_TOPIC][-1]
    pipeline.on_request_status(status.request_id, status.state, status.reason)
    assert node.dialogue.requests == [('new', 'next')]
    assert pipeline._reply_receipt_deadline is None
    assert pipeline._reply_deadline == 125.0


def test_confirmation_only_cancels_ordinary_requests_awaiting_publication(node):
    node._receive_speech(request(uid='finished', text='first'))
    node.dialogue.results = [dict(kind='answer', utterance_id='finished', text='done')]
    node._drain_dialogue()
    # Duplicate delivery can repeat the receipt but cannot resurrect pending work.
    node._receive_speech(request(uid='finished', text='first'))
    node._receive_speech(request(uid='pending', text='second'))
    node._begin_situation()
    cancelled = [s.request_id for s in node.sent[ros_communication.REQUEST_STATUS_TOPIC]
                 if s.state == 'cancelled']
    assert cancelled == ['pending']


def test_provider_cancellation_is_a_silent_terminal_status(node):
    node._receive_speech(request(uid='cancelled', text='first'))
    node.dialogue.results = [dict(kind='cancelled', utterance_id='cancelled', text='')]
    node._drain_dialogue()
    assert node.sent[ros_communication.RESPONSE_TOPIC] == []
    statuses = node.sent[ros_communication.REQUEST_STATUS_TOPIC]
    assert [(s.request_id, s.state, s.reason) for s in statuses] == [
        ('cancelled', 'accepted', ''), ('cancelled', 'cancelled', 'worker_cancelled')]
    assert node._pending_speech_requests == set()


@pytest.mark.parametrize('text,outcome', [('first', 'duplicate'), ('different', 'conflict')])
def test_cancelled_redelivery_preserves_receipt_observation_without_work(
    node, monkeypatch, text, outcome,
):
    node._receive_speech(request(uid='cancelled', text='first'))
    node._cancel_request(SimpleNamespace(request_id='cancelled'), SimpleNamespace())
    observed = []
    original = node._receipts.receive

    def receive(uid, value):
        result = original(uid, value)
        observed.append(result)
        return result

    monkeypatch.setattr(node._receipts, 'receive', receive)
    node._receive_speech(request(uid='cancelled', text=text))
    assert observed == [outcome]
    assert node.dialogue.requests == [('cancelled', 'first')]
    assert node.sent[ros_communication.RESPONSE_TOPIC] == []
    assert node.sent[ros_communication.REQUEST_STATUS_TOPIC][-1].state == 'cancelled'
    assert node._receipts.lookup('cancelled', 'first') == 'duplicate'


@pytest.mark.parametrize('error', [ValueError('invalid'), sqlite3.OperationalError('closed')])
def test_cancelled_redelivery_lookup_error_cannot_replace_terminal_state(
    node, monkeypatch, error,
):
    node._cancel_request(SimpleNamespace(request_id='cancelled'), SimpleNamespace())

    def lookup(*_args):
        raise error

    monkeypatch.setattr(node._receipts, 'lookup', lookup)
    node._receive_speech(request(uid='cancelled', text='late'))
    assert node.dialogue.requests == []
    assert node.sent[ros_communication.RESPONSE_TOPIC] == []
    assert node.sent[ros_communication.REQUEST_STATUS_TOPIC][-1].state == 'cancelled'


def test_speech_requests_have_distinct_playback_ids_and_preserve_content(node):
    """Observers can join repeated text to its own TTS playback statuses."""
    text = '  별말씀을요.\n'
    for request_type, interim in (
        (SpeechRequest.DIALOGUE, False),
        (SpeechRequest.DIALOGUE, False),
        (SpeechRequest.DIALOGUE, True),
        (SpeechRequest.NOTIFICATION, False),
    ):
        assert node.say(text, request_type=request_type, interim=interim)

    messages = node.sent[ros_communication.RESPONSE_TOPIC]
    assert len(messages) == 4
    assert all(isinstance(message.playback_id, str)
               and message.playback_id.strip() for message in messages)
    assert len({message.playback_id for message in messages}) == len(messages)
    assert all(message.request_id == '' for message in messages)
    assert [(message.text, message.request_type, message.interim)
            for message in messages] == [
        (text, SpeechRequest.DIALOGUE, False),
        (text, SpeechRequest.DIALOGUE, False),
        (text, SpeechRequest.DIALOGUE, True),
        (text, SpeechRequest.NOTIFICATION, False),
    ]


@pytest.mark.parametrize('node', [False], indirect=True)
def test_speech_endpoints_wait_for_worker_initialization(node):
    """A peer probe must not admit STT while the dialogue DB is still opening."""
    assert node.subscriptions == {}
    assert set(node.services) == {ros_communication.CANCEL_REQUEST_SERVICE}
    node._drain_dialogue()
    assert node.subscriptions == {}
    assert set(node.services) == {ros_communication.CANCEL_REQUEST_SERVICE}
    node.dialogue.ready = True
    node._drain_dialogue()
    assert set(node.subscriptions) == {
        ros_communication.TRANSCRIPT_TOPIC, '/malbut/speech/input_status',
    }
    assert set(node.services) == {
        ros_communication.ADDRESSEE_SERVICE, ros_communication.CANCEL_REQUEST_SERVICE}


@pytest.mark.parametrize('node', [False], indirect=True)
def test_failed_initialization_never_advertises_speech_endpoints(node):
    """Failed startup remains fatal without momentarily passing discovery."""
    node.dialogue.startup_error = 'DatabaseError'
    with pytest.raises(RuntimeError, match='speech_dialogue_startup_failed'):
        node._drain_dialogue()
    assert node.subscriptions == {}
    assert set(node.services) == {ros_communication.CANCEL_REQUEST_SERVICE}


def input_status(node, uid, state, session_id=''):
    callback, _ = node.subscriptions['/malbut/speech/input_status']
    callback(SpeechInputStatus(session_id=session_id, utterance_id=uid, state=state))


def test_recognition_failure_finishes_its_own_request_during_an_earlier_answer(node):
    node.dialogue.has_pending = True
    input_status(node, 'missed', 'started')
    input_status(node, 'missed', 'failed')
    messages = node.sent[ros_communication.RESPONSE_TOPIC]
    assert len(messages) == 1
    assert messages[0].request_id == 'missed'
    assert messages[0].text == '잘 알아듣지 못했어요. 다시 제이크라고 불러 주세요.'
    assert messages[0].interim is False
    assert node.dialogue.requests == []


def test_recognition_failure_speaks_once_without_model_turn_and_next_input_works(node):
    input_status(node, 'missed', 'started')
    input_status(node, 'missed', 'failed')
    input_status(node, 'missed', 'failed')
    input_status(node, 'missed', 'started')
    input_status(node, 'missed', 'failed')
    messages = node.sent[ros_communication.RESPONSE_TOPIC]
    assert len(messages) == 1
    assert messages[0].request_id == 'missed'
    assert messages[0].text == '잘 알아듣지 못했어요. 다시 제이크라고 불러 주세요.'
    assert messages[0].request_type == SpeechRequest.DIALOGUE
    assert messages[0].interim is False
    assert node.dialogue.requests == []
    assert node._receipts.lookup('missed', '미인식 원문') is None
    input_status(node, 'next', 'started')
    node._receive_speech(request(uid='next', text='다시 안녕'))
    input_status(node, 'next', 'failed')
    input_status(node, 'next', 'started')
    input_status(node, 'next', 'failed')
    assert node.dialogue.requests == [('next', '다시 안녕')]
    assert len(messages) == 1


def test_recognition_failure_ignores_stale_id_and_transcript_before_started(node):
    input_status(node, 'missing-start', 'failed')
    input_status(node, 'old', 'started')
    input_status(node, 'latest', 'started')
    input_status(node, 'old', 'started')
    input_status(node, 'old', 'failed')
    assert node.sent[ros_communication.RESPONSE_TOPIC] == []
    input_status(node, 'latest', 'failed')
    node._receive_speech(request(uid='already-transcribed', text='안녕'))
    input_status(node, 'already-transcribed', 'started')
    input_status(node, 'already-transcribed', 'failed')
    assert len(node.sent[ros_communication.RESPONSE_TOPIC]) == 1


@pytest.mark.parametrize('uid', ['', ' ', 'x' * 257])
def test_recognition_failure_ignores_invalid_ordinary_ids(node, uid):
    input_status(node, uid, 'started')
    input_status(node, uid, 'failed')
    assert node.sent[ros_communication.RESPONSE_TOPIC] == []


@pytest.mark.parametrize('blocked', ['capacity', 'startup', 'proactive'])
def test_recognition_failure_is_suppressed_when_ordinary_dialogue_is_unavailable(node, blocked):
    input_status(node, 'ordinary', 'started')
    input_status(node, 'proactive', 'started', 'incident-1')
    input_status(node, 'proactive', 'failed', 'incident-1')
    input_status(node, '', 'failed', 'incident-1')
    if blocked == 'capacity':
        node.dialogue.accept = False
    elif blocked == 'startup':
        node.dialogue.startup_error = 'Unavailable'
    else:
        node.situation.active = True
        node._begin_situation()
    input_status(node, 'ordinary', 'failed')
    assert node.sent[ros_communication.RESPONSE_TOPIC] == []
    node.dialogue.accept = True
    node.dialogue.startup_error = None
    node.situation.active = False
    node.dialogue.resume()
    input_status(node, 'ordinary', 'failed')
    assert node.sent[ros_communication.RESPONSE_TOPIC] == []


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
        message.text = 'x' * 16001
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
    assert node.sent[ros_communication.REQUEST_STATUS_TOPIC] == []
    assert messages[0].request_id == 'normal'


def test_progress_and_final_replies_share_request_id_but_not_playback_id(node):
    node.dialogue.results = [
        {'kind': 'progress', 'utterance_id': 'weather', 'text': '확인 중이에요.'},
        {'kind': 'progress', 'utterance_id': 'weather', 'text': '다시 확인할게요.'},
        {'kind': 'answer', 'utterance_id': 'weather', 'text': '오늘은 맑아요.'},
        {'kind': 'progress', 'utterance_id': 'other', 'text': '잠시 기다려 주세요.'},
        {'kind': 'error', 'utterance_id': 'other', 'text': '요청을 처리하지 못했어요.'},
    ]
    node._drain_dialogue()
    messages = node.sent[ros_communication.RESPONSE_TOPIC]
    assert [(message.request_id, message.interim) for message in messages] == [
        ('weather', True), ('weather', True), ('weather', False),
        ('other', True), ('other', False),
    ]
    assert len({message.playback_id for message in messages}) == 5


def test_overlong_final_speech_is_rejected_silently_without_receipt(node):
    text = '가' * 16001
    node._receive_speech(SimpleNamespace(utterance_id='too-long', text=text))
    assert node._receipts.lookup('too-long', text) is None
    assert node.sent[ros_communication.RESPONSE_TOPIC] == []
    statuses = node.sent[ros_communication.REQUEST_STATUS_TOPIC]
    assert [(s.request_id, s.state, s.reason) for s in statuses] == [
        ('too-long', 'rejected', 'text_too_long')]
    assert node.dialogue.requests == []


@pytest.mark.parametrize('failure', ['startup', 'capacity', 'submit', 'lookup', 'store'])
def test_rejected_speech_sends_correlated_silent_terminal_status(node, monkeypatch, failure):
    """Every failed new turn can release STT's matching response wait."""
    def storage_error(*_args):
        raise sqlite3.OperationalError('receipt unavailable')

    if failure == 'startup':
        node.dialogue.startup_error = 'Unavailable'
    elif failure == 'capacity':
        node.dialogue.accept = False
    elif failure == 'submit':
        monkeypatch.setattr(node.dialogue, 'submit', lambda *_: False)
    elif failure == 'lookup':
        monkeypatch.setattr(node._receipts, 'lookup', storage_error)
    else:
        monkeypatch.setattr(node._receipts, 'receive', storage_error)

    node._receive_speech(request(uid='rejected', text='안녕'))

    assert node.sent[ros_communication.RESPONSE_TOPIC] == []
    expected = {
        'startup': ('rejected', 'not_ready'),
        'capacity': ('rejected', 'busy'),
        'submit': ('failed', 'submission_failed'),
        'lookup': ('rejected', 'receipt_unavailable'),
        'store': ('rejected', 'receipt_unavailable'),
    }
    statuses = node.sent[ros_communication.REQUEST_STATUS_TOPIC]
    assert [(s.request_id, s.state, s.reason) for s in statuses] == [
        ('rejected', *expected[failure])]
    assert node.dialogue.requests == []


@pytest.mark.parametrize('text', ['안녕', '다른 내용'])
def test_previously_received_speech_does_not_send_another_final_notice(node, text):
    node._receipts.receive('existing', '안녕')
    node._receive_speech(request(uid='existing', text=text))
    assert node.sent[ros_communication.RESPONSE_TOPIC] == []
    assert node.dialogue.requests == []


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
