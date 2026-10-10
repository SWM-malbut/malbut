"""Check PCM notices, cancellation, missing files, and zero synthesis calls."""

from threading import Event
import wave

import numpy as np
import pytest

from malbut_tts.prerecorded import PrerecordedAudio
from malbut_tts.prepare_notices import prepare
from malbut_tts.runtime import SpeechRuntime


def write_wav(path, *, channels=1, rate=24000, frames=6000):
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(np.full(frames * channels, 16384, dtype='<i2').tobytes())


def test_wav_streaming_and_cancel_between_chunks(tmp_path):
    write_wav(tmp_path / 'patrol.failed.camera_stale.wav')
    source = PrerecordedAudio(tmp_path)
    cancel = Event()
    chunks = source.generate('patrol.failed.camera_stale', cancel)
    pcm, rate = next(chunks)
    assert rate == 24000 and len(pcm) == 2400
    np.testing.assert_array_equal(pcm, np.full(2400, 0.5, dtype=np.float32))
    cancel.set()
    assert list(chunks) == []


@pytest.mark.parametrize('audio_id', ['../secret', '/secret', '..', 'x/y', 'a' * 129])
def test_id_is_never_a_filesystem_path(tmp_path, audio_id):
    with pytest.raises(ValueError):
        list(PrerecordedAudio(tmp_path).generate(audio_id, Event()))


def test_symlink_cannot_escape_notice_directory(tmp_path):
    directory = tmp_path / 'audio'
    directory.mkdir()
    write_wav(tmp_path / 'outside.wav')
    (directory / 'notice.wav').symlink_to(tmp_path / 'outside.wav')
    with pytest.raises(ValueError):
        list(PrerecordedAudio(directory).generate('notice', Event()))


@pytest.mark.parametrize('options', [{'channels': 2}, {'rate': 16000}, {'frames': 0}])
def test_invalid_audio_is_rejected_before_playback(tmp_path, options):
    write_wav(tmp_path / 'notice.wav', **options)
    with pytest.raises(ValueError):
        list(PrerecordedAudio(tmp_path).generate('notice', Event()))


def test_truncated_audio_is_rejected_before_first_chunk(tmp_path):
    path = tmp_path / 'notice.wav'
    write_wav(path)
    path.write_bytes(path.read_bytes()[:-2])
    with pytest.raises(ValueError, match='truncated'):
        next(PrerecordedAudio(tmp_path).generate('notice', Event()))


@pytest.mark.parametrize('exists', [True, False])
def test_runtime_never_calls_synthesis_for_file_or_missing_file(tmp_path, exists):
    from test_runtime import FakePlayer, FakeSynthesizer
    if exists:
        write_wav(tmp_path / 'notice.wav')
    finished = Event()
    statuses = []
    synth = FakeSynthesizer()
    def player_factory(**kwargs):
        player = FakePlayer(**kwargs)
        player.drain.set()
        return player
    def status(pid, state, interim, request_id):
        statuses.append(state)
        if state in ('finished', 'failed'):
            finished.set()
    runtime = SpeechRuntime(synth, player_factory, status, prerecorded=PrerecordedAudio(tmp_path))
    try:
        runtime.submit('camera failure transcript', audio_id='notice')
        assert finished.wait(3)
        assert statuses[-1] == ('finished' if exists else 'failed')
        assert synth.texts == []
    finally:
        runtime.close()


def test_preparation_resumes_valid_files_and_detects_changed_text(tmp_path):
    class Synth:
        def generate(self, text, cancel):
            yield np.zeros(2400, dtype=np.float32), 24000
    catalog = {'notice': '고정 안내'}
    assert prepare(catalog, tmp_path, Synth()) == 1
    assert prepare(catalog, tmp_path, check_only=True) == 0
    assert prepare(catalog, tmp_path) == 0
    with pytest.raises(ValueError, match='does not match'):
        prepare({'notice': '바뀐 안내'}, tmp_path, Synth())
    with pytest.raises(ValueError, match='does not match'):
        prepare(catalog, tmp_path, Synth(), instructions='A different voice style')


def test_queued_start_recording_plays_before_fast_final_without_synthesis(tmp_path):
    from test_runtime import Harness
    write_wav(tmp_path / 'start.wav')
    harness = Harness(prerecorded=PrerecordedAudio(tmp_path))
    try:
        blocker = harness.runtime.submit('earlier speech')
        first = harness.players.get(timeout=3)
        assert first.finishing.wait(3)
        start = harness.runtime.submit('네, 날씨 조회를 시작하겠습니다.', audio_id='start',
                                       interim=True, request_id='ack:weather')
        final = harness.runtime.submit('맑아요.', request_id='weather')
        first.drain.set()
        harness.wait(blocker, 'finished')
        player = harness.players.get(timeout=3)
        assert player.finishing.wait(3)
        assert harness.synth.texts == ['earlier speech']
        player.drain.set()
        harness.wait(start, 'finished')
        player = harness.players.get(timeout=3)
        player.drain.set()
        harness.wait(final, 'finished')
        assert harness.synth.texts == ['earlier speech', '맑아요.']
    finally:
        harness.runtime.close()


def test_api_failure_notice_is_not_saved_as_the_requested_recording(tmp_path):
    class Synth:
        def generate(self, text, cancel):
            yield np.zeros(2400, dtype=np.float32), 24000
            return 'notice_substituted'
    with pytest.raises(ValueError, match='API error audio'):
        prepare({'notice': '작업 완료'}, tmp_path, Synth())
    assert not (tmp_path / 'notice.wav').exists()
    assert not (tmp_path / 'manifest.json').exists()
