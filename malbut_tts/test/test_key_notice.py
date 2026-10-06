"""SWM25-235: the key the owner sets on the web, and the notice when it cannot be used.

Offline: check the bundled recording and exercise failures with short test WAVs.
"""

import json
from threading import Event
from types import SimpleNamespace
import wave

import numpy as np
import pytest

from malbut_tts.api_synthesis import ApiTtsError, OpenAISynthesizer
from malbut_tts.managed_key import OpenAIKey
from malbut_tts.runtime import CONFIRMATION, DIALOGUE, SpeechRuntime

TEAM = 'team-test-key-0001'
OWNER = 'owner-test-key-0002'
NOTICE_TEXT = '지금은 대화를 할 수 없어요.'


def write_notice(path, samples=6000, rate=24000, channels=1, width=2):
    with wave.open(str(path), 'wb') as file:
        file.setnchannels(channels)
        file.setsampwidth(width)
        file.setframerate(rate)
        file.writeframes((np.arange(samples * channels) % 100).astype('<i2').tobytes()
                         if width == 2 else b'\x00' * samples * channels * width)
    return path


class Client:
    """One fake streaming request: an error at headers, or PCM chunks."""

    def __init__(self, error=None, chunks=(b'\x00\x10' * 9600,)):
        self.error, self.chunks, self.keys = error, chunks, []
        self.audio = SimpleNamespace(speech=SimpleNamespace(with_streaming_response=self))
        self.headers = {'content-type': 'audio/pcm'}

    def __call__(self, **kwargs):
        self.keys.append(kwargs['api_key'])
        return self

    def create(self, **kwargs):
        return self

    async def __aenter__(self):
        if self.error is not None:
            raise self.error
        return self

    async def __aexit__(self, *args):
        pass

    async def close(self):
        pass

    async def iter_bytes(self, *, chunk_size):
        for chunk in self.chunks:
            yield chunk


def status_error(status, code=None):
    error = RuntimeError('private provider body')
    error.status_code = status
    if code is not None:
        error.code = code
    return error


def key_in(tmp_path, team=TEAM):
    return OpenAIKey(environ={'OPENAI_API_KEY': team}, directory=tmp_path)


def web_sets(tmp_path, version, key=None):
    if key is not None:
        (tmp_path / 'openai.key').write_text(key + '\n')
    (tmp_path / 'openai.key.version').write_text(
        json.dumps({'keyVersion': version, 'deleted': key is None}))


def test_key_rules_match_the_agent(tmp_path):
    key = key_in(tmp_path)
    assert key.current() == TEAM
    web_sets(tmp_path, 1, OWNER)
    assert key.current() == OWNER
    (tmp_path / 'openai.key').unlink()
    web_sets(tmp_path, 2)
    assert key.current() == ''
    assert TEAM not in repr(key)


def test_every_utterance_uses_the_key_in_effect_then(tmp_path):
    key = key_in(tmp_path)
    client = Client()
    synth = OpenAISynthesizer(api_key=key, client_factory=client, notice_path=None)
    list(synth.generate('첫 문장', Event()))
    web_sets(tmp_path, 1, OWNER)
    list(synth.generate('둘째 문장', Event()))
    assert client.keys == [TEAM, OWNER]
    assert key.health == ('ok', None)


@pytest.mark.parametrize('error, health', [
    (status_error(401), ('invalid', 'authentication_failed')),
    (status_error(403), ('invalid', 'permission_denied')),
    (status_error(429, 'insufficient_quota'), ('quota', 'insufficient_quota')),
])
def test_key_failure_plays_the_notice_through_the_same_stream(tmp_path, error, health):
    notice = write_notice(tmp_path / 'notice.wav', samples=6000)
    key = key_in(tmp_path)
    synth = OpenAISynthesizer(api_key=key, client_factory=Client(error), notice_path=notice)
    chunks = list(synth.generate('대체 대답', Event()))
    assert [len(audio) for audio, _ in chunks] == [2400, 2400, 1200]
    assert {rate for _, rate in chunks} == {24000}
    assert all(audio.dtype == np.float32 for audio, _ in chunks)
    assert key.health == health


def test_no_key_plays_the_notice_without_any_request(tmp_path):
    notice = write_notice(tmp_path / 'notice.wav')
    key = key_in(tmp_path, team='')
    client = Client()
    synth = OpenAISynthesizer(api_key=key, client_factory=client, notice_path=notice)
    assert sum(len(audio) for audio, _ in synth.generate('안녕', Event())) == 6000
    assert client.keys == [] and key.health == ('missing', 'missing_api_key')


@pytest.mark.parametrize('key', ['', TEAM])
def test_explicit_notice_uses_the_bundled_recording_without_api(key):
    client = Client()
    synth = OpenAISynthesizer(api_key=key, client_factory=client)
    chunks = list(synth.generate(NOTICE_TEXT, Event()))
    assert client.keys == []
    assert chunks and all(audio.ndim == 1 and audio.dtype == np.float32
                          for audio, _ in chunks)
    assert 0 < sum(len(audio) / rate for audio, rate in chunks) <= 30


def test_explicit_notice_missing_file_fails_without_api(tmp_path):
    client = Client()
    synth = OpenAISynthesizer(api_key=TEAM, client_factory=client,
                              notice_path=tmp_path / 'missing.wav')
    with pytest.raises(ApiTtsError, match='notice_unavailable'):
        list(synth.generate(NOTICE_TEXT, Event()))
    assert client.keys == []


@pytest.mark.parametrize('error, code', [
    (status_error(429), 'rate_limited'), (status_error(500), 'provider_error'),
    (TimeoutError(), 'timeout'),
])
def test_other_failures_stay_failures_without_notice_or_health(tmp_path, error, code):
    notice = write_notice(tmp_path / 'notice.wav')
    key = key_in(tmp_path)
    synth = OpenAISynthesizer(api_key=key, client_factory=Client(error), notice_path=notice)
    with pytest.raises(ApiTtsError, match=f'^API TTS failed: {code}$'):
        list(synth.generate('안녕', Event()))
    assert key.health is None


def test_key_failure_after_partial_audio_does_not_append_notice(tmp_path):
    class PartialClient(Client):
        async def iter_bytes(self, *, chunk_size):
            yield b'\x00\x10' * 9600
            raise status_error(401)

    synth = OpenAISynthesizer(
        api_key=TEAM, client_factory=PartialClient(),
        notice_path=write_notice(tmp_path / 'notice.wav'),
    )
    chunks = synth.generate('원래 답변', Event())
    assert len(next(chunks)[0]) == 9600
    with pytest.raises(ApiTtsError, match='authentication_failed'):
        next(chunks)


@pytest.mark.parametrize('notice', [
    None, 'missing.wav', ('stereo.wav', {'channels': 2}), ('8bit.wav', {'width': 1}),
    ('long.wav', {'samples': 24000 * 31}), ('empty.wav', {'samples': 0}),
])
def test_missing_or_unusable_notice_keeps_the_old_failure(tmp_path, notice):
    if isinstance(notice, tuple):
        name, options = notice
        notice = write_notice(tmp_path / name, **options)
    elif notice is not None:
        notice = tmp_path / notice
    synth = OpenAISynthesizer(api_key=key_in(tmp_path), client_factory=Client(status_error(401)),
                              notice_path=notice)
    with pytest.raises(ApiTtsError, match='^API TTS failed: authentication_failed$'):
        list(synth.generate('안녕', Event()))


def test_stop_during_the_notice_ends_it(tmp_path):
    notice = write_notice(tmp_path / 'notice.wav', samples=24000)
    synth = OpenAISynthesizer(api_key=key_in(tmp_path, team=''), client_factory=Client(),
                              notice_path=notice)
    cancel = Event()
    chunks = synth.generate('안녕', cancel)
    next(chunks)
    cancel.set()
    assert list(chunks) == []


def test_tts_node_shares_key_health_without_the_key(monkeypatch):
    """The node publishes only {service, state, code} on /malbut/keys/health."""
    import sys
    from types import ModuleType

    from malbut_tts import node as tts_node

    published, timers, logs = [], [], []

    class FakeNode:
        def __init__(self, name):
            self.parameters = {}

        def declare_parameter(self, name, default):
            self.parameters.setdefault(name, default)

        def get_parameter(self, name):
            return SimpleNamespace(value=self.parameters[name])

        def get_logger(self):
            return SimpleNamespace(info=logs.append, warning=logs.append)

        def create_publisher(self, message_type, topic, qos):
            return SimpleNamespace(publish=lambda message: published.append((topic, qos, message)))

        def create_subscription(self, *args):
            pass

        def create_service(self, *args):
            pass

        def create_timer(self, interval, callback):
            timers.append(callback)

        def destroy_node(self):
            return True

    key = OpenAIKey(environ={'OPENAI_API_KEY': TEAM}, directory='/nonexistent')
    synthesizer = SimpleNamespace(key=key)
    std_messages = ModuleType('std_msgs.msg')
    std_messages.String = lambda data: SimpleNamespace(data=data)
    for name, module in {
        'rclpy': ModuleType('rclpy'), 'rclpy.node': SimpleNamespace(Node=FakeNode),
        'rclpy.qos': SimpleNamespace(
            DurabilityPolicy=SimpleNamespace(VOLATILE='volatile', TRANSIENT_LOCAL='transient_local'),
            HistoryPolicy=SimpleNamespace(KEEP_LAST='keep_last'),
            ReliabilityPolicy=SimpleNamespace(RELIABLE='reliable'),
            QoSProfile=lambda **kwargs: kwargs),
        'std_msgs': ModuleType('std_msgs'), 'std_msgs.msg': std_messages,
        'malbut_interfaces': ModuleType('malbut_interfaces'),
        'malbut_interfaces.msg': SimpleNamespace(SpeechRequest=object,
                                                 SpeechPlaybackStatus=SimpleNamespace),
        'malbut_interfaces.srv': SimpleNamespace(ControlSpeechPlayback=object),
        'malbut_tts.backends': SimpleNamespace(create_synthesizer=lambda *a, **k: synthesizer),
        'malbut_tts.audio': SimpleNamespace(StreamingPlayer=object),
        'malbut_tts.runtime': SimpleNamespace(SpeechRuntime=lambda *a, **k: SimpleNamespace(
            close=lambda: None)),
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    node = tts_node.create_tts_node()
    key.report('invalid', 'authentication_failed')
    key.report('invalid', 'authentication_failed')
    for timer in timers:
        timer()
    health = [(topic, qos, message.data) for topic, qos, message in published
              if topic == tts_node.KEY_HEALTH_TOPIC]
    assert health == [(
        '/malbut/keys/health',
        {'depth': 4, 'reliability': 'reliable', 'durability': 'transient_local'},
        '{"service":"openai","state":"invalid","code":"authentication_failed"}')]
    assert any('authentication_failed' in str(line) for line in logs)
    assert not any('playing' in str(line) for line in logs)
    assert not any(TEAM in str(line) for line in logs)
    node.destroy_node()


@pytest.mark.parametrize('text, request_type, stop, terminal', [
    (NOTICE_TEXT, DIALOGUE, False, 'finished'),
    ('원래 답변', DIALOGUE, False, 'failed'),
    ('넘어지셨나요?', CONFIRMATION, False, 'failed'),
    ('넘어지셨나요?', CONFIRMATION, True, 'stopped'),
    (NOTICE_TEXT, DIALOGUE, True, 'stopped'),
])
def test_notice_terminal_follows_device_drain(tmp_path, text, request_type, stop, terminal):
    """A substituted question never finishes; its notice still drains or obeys STOP."""
    notice = write_notice(tmp_path / 'notice.wav', samples=4800)
    synth = OpenAISynthesizer(api_key=key_in(tmp_path), client_factory=Client(status_error(401)),
                              notice_path=notice)
    written, statuses, done = [], [], Event()
    finishing, drain = Event(), Event()

    class Player:
        def __init__(self, on_state, cancel_event):
            self.on_state, self.cancel = on_state, cancel_event

        def write(self, audio, rate):
            written.append((len(audio), rate))
            self.on_state('playing')

        def finish(self):
            finishing.set()
            assert drain.wait(3), 'test did not release device drain'

        def stop(self):
            self.cancel.set()
            drain.set()

        def close(self):
            drain.set()

    def status(playback_id, state, interim, request_id):
        statuses.append((playback_id, state, interim, request_id))
        if state in ('finished', 'failed', 'stopped'):
            done.set()

    runtime = SpeechRuntime(synth, Player, status)
    request_id = '' if request_type == CONFIRMATION else 'utterance-1'
    try:
        runtime.submit(text, request_type, playback_id='notice-1', request_id='utterance-1')
        assert finishing.wait(2)
        assert statuses == [('notice-1', 'playing', False, request_id)]
        assert not done.is_set()
        if stop:
            assert runtime.control('notice-1', 'stop')
        else:
            drain.set()
        assert done.wait(2)
    finally:
        runtime.close()
    assert statuses == [
        ('notice-1', 'playing', False, request_id),
        ('notice-1', terminal, False, request_id),
    ]
    assert written == [(2400, 24000), (2400, 24000)]
