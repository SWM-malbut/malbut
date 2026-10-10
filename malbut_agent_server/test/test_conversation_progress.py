"""One request-wide retry and speech notices that cannot outlive their turn."""

from concurrent.futures import CancelledError
import threading

import pytest

from malbut_agent_server import conversation_progress as progress
from malbut_agent_server.function_speech import FUNCTION_STARTS
from malbut_agent_server.config import Settings
from malbut_agent_server.factory import build_orchestrator
from malbut_agent_server.providers.base import ProviderError
from malbut_agent_server.providers.reliable import ReliableProvider
from malbut_agent_server.schemas import AgentDecision
from malbut_agent_server.speech_dialogue import DialogueWorker
from test_reliable_provider import _ScriptedProvider, _message_result, _complete
from test_speech_dialogue import FixedProvider, RuntimeFactory, collect, wait_until
from test_weather_dialogue import WeatherProvider, request


def test_request_retry_budget_includes_fallback_and_cancel_never_retries():
    notices = []
    primary = _ScriptedProvider([TimeoutError(), TimeoutError(), _message_result()])
    fallback = _ScriptedProvider([_message_result()])
    provider = ReliableProvider([primary, fallback], max_retries=3, sleep=lambda _: None)
    with progress.request_scope(lambda text, state: notices.append(text)):
        result = _complete(provider)
    assert result.provider == 'reliable-fallback'
    assert primary.call_count == 2 and fallback.call_count == 0
    assert notices == [progress.MODEL_RETRY_NOTICE]

    malformed = _ScriptedProvider([ProviderError('bad JSON'), _message_result()])
    with progress.request_scope():
        assert _complete(ReliableProvider([malformed], sleep=lambda _: None)).provider == 'test'
    assert malformed.call_count == 2

    canceled = _ScriptedProvider([CancelledError(), _message_result()])
    with progress.request_scope(), pytest.raises(CancelledError):
        _complete(ReliableProvider([canceled, fallback]))
    assert canceled.call_count == 1 and fallback.call_count == 0


@pytest.mark.parametrize('failure_first', ['model', 'weather'])
def test_model_and_weather_share_one_request_retry(failure_first):
    class Provider(WeatherProvider):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def complete(self, *args, weather_context=None, **kwargs):
            self.attempts += 1
            if ((failure_first == 'model' and self.attempts == 1)
                    or (failure_first == 'weather' and weather_context is not None)):
                raise TimeoutError()
            return super().complete(*args, weather_context=weather_context, **kwargs)

    runtime = build_orchestrator(Settings(database_path=':memory:'), http_server=False)
    runtime.conversation_store.create('weather-user', 'weather-conversation')
    provider = Provider()
    reads, notices = [], []
    runtime.provider = ReliableProvider([provider], max_retries=3, sleep=lambda _: None)
    runtime.weather_executor = lambda read_id: (reads.append(read_id) or {'status': 'unavailable'})
    try:
        with progress.request_scope(lambda text, state: notices.append(text)):
            runtime.handle(request())
        assert provider.attempts == (2 if failure_first == 'model' else 1)
        assert len(set(reads)) == (1 if failure_first == 'model' else 2)
        assert notices == ([progress.MODEL_RETRY_NOTICE, FUNCTION_STARTS['get_weather']]
                           if failure_first == 'model' else
                           [FUNCTION_STARTS['get_weather'], progress.WEATHER_RETRY_NOTICE])
    finally:
        runtime.close()


@pytest.mark.parametrize('suspend', [False, True])
def test_fast_function_receipt_survives_answer_but_not_suspension(suspend):
    def answer(request, history):
        progress.announce_function_start('get_weather')
        progress.announce_function_start('get_weather')
        return FixedProvider.answer(request, history)

    worker = DialogueWorker(RuntimeFactory(FixedProvider(answer)), 'user', capacity=1)
    try:
        assert worker.submit('quick-weather', '날씨 조회해 줘')
        wait_until(lambda: len(worker._results) == 2)
        replies = worker.drain()
        assert [reply['kind'] for reply in replies] == ['acknowledgement', 'answer']
        assert worker.has_capacity()
        if suspend:
            worker.suspend()
            worker.resume()
        spoken = []
        for reply in replies:
            worker.publish_reply(reply, lambda text: spoken.append(text) or True)
        assert spoken == ([] if suspend else [FUNCTION_STARTS['get_weather'], replies[1]['text']])
        assert worker.publish_reply(replies[0], lambda text: spoken.append(text) or True) is None
    finally:
        worker.close()


def test_cancelled_function_receipt_cannot_publish_after_scope():
    notices = []
    with pytest.raises(CancelledError):
        with progress.request_scope(lambda text, state: notices.append((text, state))):
            progress.announce_function_start('get_weather')
            raise CancelledError()
    assert len(notices) == 1
    text, state = notices[0]
    assert not state.publish(lambda _: pytest.fail('cancelled receipt published'), text)


def test_function_start_suppresses_pending_delay_and_repeated_start():
    notices = []
    state = progress.RequestProgress(lambda text, _: notices.append(text))
    try:
        state.start_function('get_weather')
        state.start_function('get_weather')
        assert state._timer.finished.is_set()
        assert notices == [FUNCTION_STARTS['get_weather']]
        assert not state.publish(lambda _: pytest.fail('extra delay notice'), progress.DELAY_NOTICE)
    finally:
        state.finish()


class Timer:
    instances = []

    def __init__(self, seconds, callback, args):
        self.seconds, self.callback, self.args = seconds, callback, args
        self.canceled = False
        self.instances.append(self)

    def start(self):
        pass

    def cancel(self):
        self.canceled = True

    def fire(self):
        self.callback(*self.args)


@pytest.mark.parametrize('finish', ['answer', 'close'])
def test_speech_progress_does_not_release_capacity_or_publish_after_finish(monkeypatch, finish):
    Timer.instances = []
    monkeypatch.setattr(progress, 'Timer', Timer)
    release = threading.Event()

    def answer(request, history):
        assert release.wait(5)
        return FixedProvider.answer(request, history)

    provider = FixedProvider(answer)
    worker = DialogueWorker(RuntimeFactory(provider), 'user', capacity=1)
    closer = None
    try:
        assert worker.submit('utterance-1', '안녕')
        assert provider.entered.wait(5)
        timer = Timer.instances[0]
        assert timer.seconds == 5.0
        timer.fire()
        notice = collect(worker, 1)[0]
        assert notice['kind'] == 'progress' and notice['utterance_id'] == 'utterance-1'
        assert not worker.has_capacity()
        published = []
        assert worker.publish_reply(notice, lambda text: published.append(text) or True)
        assert published == [progress.DELAY_NOTICE]
        if finish == 'close':
            closer = threading.Thread(target=worker.close)
            closer.start()
            wait_until(lambda: timer.canceled)
        release.set()
        if closer is None:
            assert collect(worker, 1)[0]['kind'] == 'answer'
            assert worker.has_capacity()
        else:
            closer.join(5)
            assert not closer.is_alive()
        assert timer.canceled
        timer.fire()
        assert worker.drain() == []
        assert worker.publish_reply(notice, lambda text: published.append(text) or True) is None
        assert published == [progress.DELAY_NOTICE]
    finally:
        release.set()
        worker.close()
        if closer is not None:
            closer.join(5)


def test_waiting_speech_notice_starts_at_admission_and_shutdown_cancels_queue(monkeypatch):
    Timer.instances = []
    monkeypatch.setattr(progress, 'Timer', Timer)
    release = threading.Event()

    def answer(request, history):
        assert release.wait(5)
        return FixedProvider.answer(request, history)

    provider = FixedProvider(answer)
    worker = DialogueWorker(RuntimeFactory(provider), 'user', capacity=2)
    closer = None
    try:
        assert worker.submit('first', '안녕')
        assert provider.entered.wait(5)
        assert worker.submit('waiting', '기다리는 다음 질문')
        assert len(Timer.instances) == 2 and len(provider.calls) == 1
        Timer.instances[1].fire()
        notice = collect(worker, 1)[0]
        assert notice['kind'] == 'progress' and notice['utterance_id'] == 'waiting'
        assert not worker.has_capacity()
        closer = threading.Thread(target=worker.close)
        closer.start()
        wait_until(lambda: all(timer.canceled for timer in Timer.instances))
        release.set()
        closer.join(5)
        assert not closer.is_alive() and len(provider.calls) == 1
        assert worker.publish_reply(notice, lambda text: pytest.fail('late notice')) is None
    finally:
        release.set()
        worker.close()
        if closer is not None:
            closer.join(5)


def test_fast_speech_retry_is_reported_once_in_final_answer():
    provider = _ScriptedProvider([TimeoutError(), _message_result()])
    worker = DialogueWorker(RuntimeFactory(
        ReliableProvider([provider], sleep=lambda _: None),
    ), 'user')
    try:
        assert worker.submit('retry-utterance', '안녕')
        wait_until(lambda: provider.call_count == 2 and worker._active_progress is None)
        replies = collect(worker, 1)
        assert len(replies) == 1 and replies[0]['kind'] == 'answer'
        assert replies[0]['text'] == '답변 생성을 한 번 다시 시도했어요. 정상 응답'
        assert provider.call_count == 2 and worker.drain() == []
    finally:
        worker.close()

    published = []
    state = progress.RequestProgress()
    assert state.retry(progress.MODEL_RETRY_NOTICE)
    assert state.publish(lambda text: published.append(text) or True, progress.MODEL_RETRY_NOTICE)
    state.finish()
    assert state.final_text('정상 응답') == '정상 응답'
    assert published == [progress.MODEL_RETRY_NOTICE]


@pytest.mark.parametrize('notice', [progress.MODEL_RETRY_NOTICE, progress.WEATHER_RETRY_NOTICE])
@pytest.mark.parametrize('published', [False, True])
def test_offline_failure_notice_stays_exact_before_and_after_retry_publication(notice, published):
    release = threading.Event()

    def answer(request, history):
        assert progress.claim_retry(notice)
        assert release.wait(5)
        return AgentDecision('refusal', progress.SERVICE_UNAVAILABLE_NOTICE,
                             reason='provider_unavailable')

    provider = FixedProvider(answer)
    worker = DialogueWorker(RuntimeFactory(provider), 'user')
    try:
        assert worker.submit('failed-utterance', '안녕')
        assert provider.entered.wait(5)
        if published:
            interim = collect(worker, 1)[0]
            assert interim['kind'] == 'progress' and interim['text'] == notice
            assert worker.publish_reply(interim, lambda _: True)
        release.set()
        wait_until(lambda: worker._active_progress is None)
        reply = collect(worker, 1)[0]
        assert reply['kind'] == 'answer' and reply['utterance_id'] == 'failed-utterance'
        assert reply['text'] == '지금은 대화를 할 수 없어요.'
        spoken = []
        assert worker.publish_reply(reply, lambda text: spoken.append(text) or True)
        assert spoken == ['지금은 대화를 할 수 없어요.']
        assert worker.drain() == []
    finally:
        release.set()
        worker.close()
