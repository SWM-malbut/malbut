"""Silent single-worker speculation and exact final reuse, without a model."""

from threading import Event, get_ident
from types import SimpleNamespace

import numpy as np
import pytest

from malbut_tts.live_piper_synthesis import LivePiperSynthesizer
from malbut_tts.prefetch import MAX_PREPARED_BYTES, PrefetchSlot, preparation_key
from test_runtime import FakePlayer, Harness


PREFIX = '첫 문장입니다. 다'
FINAL = '첫 문장입니다. 다음 문장입니다.'
KEY = dict(request_id='turn', generation_id='generation', prepared_id='prepared')


class Player(FakePlayer):
    def wait_for_capacity(self, **_):
        pass


class PrefetchHarness(Harness):
    def make_player(self, **kwargs):
        player = Player(**kwargs)
        self.players.put(player)
        return player


def engine(synthesize=None):
    calls = []
    def default(text, _config):
        calls.append((text, get_ident()))
        yield SimpleNamespace(audio_float_array=np.ones(32, dtype=np.float32))
    synth = LivePiperSynthesizer('/fixture/voice.onnx')
    synth._voice = SimpleNamespace(synthesize=synthesize or default)
    synth._normalizer = lambda text: text
    synth._synthesis = object()
    synth.sample_rate = 22050
    return synth, calls


def ready(h):
    with h.runtime._condition:
        assert h.runtime._condition.wait_for(
            lambda: h.runtime._prefetch.job is not None
            and h.runtime._prefetch.job.state == 'ready', 3)


def finish(h, text=FINAL, **options):
    identity = h.runtime.submit(text, **options)
    player = h.active(identity)
    player.drain.set()
    h.wait(identity, 'finished')
    return player


def test_default_disabled_does_not_prepare_or_create_player():
    synth, calls = engine()
    h = PrefetchHarness(synth)
    try:
        assert not h.runtime.prepare(PREFIX, **KEY)
        assert calls == [] and h.events == [] and h.players.empty()
    finally:
        h.runtime.close()


@pytest.mark.parametrize('new_speed,expected_lengths', [
    (0.9, [1.0, 1.0]), (1.8, [1.0, 0.5, 0.5]),
])
def test_prepared_audio_matches_final_speed_and_uses_final_volume(new_speed, expected_lengths):
    lengths = []

    def synthesize(text, config):
        lengths.append(config.length_scale)
        yield SimpleNamespace(audio_float_array=np.ones(32, dtype=np.float32))

    synth, _ = engine(synthesize)
    synth._voice.config = SimpleNamespace(length_scale=0.9)
    synth._synthesis = SimpleNamespace(length_scale=1.0)
    settings = (0.9, 100)
    h = PrefetchHarness(synth, prefetch_enabled=True, voice_settings=lambda: settings)
    try:
        assert h.runtime.prepare(PREFIX, **KEY)
        ready(h)
        settings = (new_speed, 25)
        player = finish(h, **KEY)
        assert lengths == pytest.approx(expected_lengths)
        assert len(player.audio) == 2
        for samples, _ in player.audio:
            np.testing.assert_allclose(samples, 0.25)
    finally:
        h.runtime.close()


def test_silent_first_segment_is_consumed_once_without_duplicate_audio():
    synth, calls = engine()
    h = PrefetchHarness(synth, prefetch_enabled=True)
    try:
        assert h.runtime.prepare(PREFIX, **KEY)
        ready(h)
        assert h.events == [] and h.players.empty()
        assert [text for text, _ in calls] == ['첫 문장입니다. ']
        player = finish(h, **KEY)
        assert [text for text, _ in calls] == ['첫 문장입니다. ', '다음 문장입니다.']
        assert len(player.audio) == 2 and not player.audio[0][0].flags.writeable
        assert len({thread for _, thread in calls}) == 1
        assert h.runtime._prefetch.job is None
        # A repeated hint has no reusable bytes left; all normal text is spoken.
        second = finish(h, **KEY)
        assert len(calls) == 4 and len(second.audio) == 2
        assert not h.runtime.prepare(PREFIX, **KEY)  # Already finalized turn.
    finally:
        h.runtime.close()


def test_final_before_worker_starts_discards_redundant_preparation():
    synth, calls = engine()
    h = PrefetchHarness(synth, prefetch_enabled=True)
    try:
        # Hold admission until both messages arrive, so the worker cannot start
        # speculation between PREPARE and the already committed final request.
        with h.runtime._condition:
            assert h.runtime.prepare(PREFIX, **KEY)
            identity = h.runtime.submit(FINAL, **KEY)
            assert h.runtime._prefetch.job is None
        player = h.active(identity)
        player.drain.set()
        h.wait(identity, 'finished')
        assert [text for text, _ in calls] == ['첫 문장입니다. ', '다음 문장입니다.']
        assert len(player.audio) == 2
    finally:
        h.runtime.close()


@pytest.mark.parametrize('change', ['source', 'generation', 'token', 'config', 'normalization', 'expiry'])
def test_mismatches_are_cache_misses_not_missing_answers(change):
    synth, calls = engine()
    now = [0.]
    h = PrefetchHarness(synth, prefetch_enabled=True, clock=lambda: now[0])
    try:
        h.runtime.prepare(PREFIX, **KEY);ready(h)
        text = FINAL; options = dict(KEY)
        if change == 'source': text = '첫 문장입니다. 별도 문장입니다.'
        if change == 'generation': options['generation_id'] = 'new-generation'
        if change == 'token': options['prepared_id'] = 'new-token'
        if change == 'config': synth._speed = 1.
        if change == 'normalization': synth._normalizer = lambda value: value.replace('첫', '첫째')
        if change == 'expiry': now[0] = 9.
        player = finish(h, text, **options)
        assert len(calls) == 3 and len(player.audio) == 2
    finally:
        h.runtime.close()


def test_final_validation_still_precedes_any_prepared_audio_write():
    synth, _ = engine()
    h = PrefetchHarness(synth, prefetch_enabled=True)
    try:
        h.runtime.prepare(PREFIX, **KEY);ready(h)
        def reject():
            raise ValueError('memory changed')
        identity = h.runtime.submit(FINAL, validate=reject, **KEY)
        h.wait(identity, 'failed')
        assert h.players.get(timeout=1).audio == []
        assert not any(state == 'playing' for _, state in h.events)
    finally:
        h.runtime.close()


@pytest.mark.parametrize('action', ['cancel', 'stop', 'stop_all', 'close'])
def test_running_prepare_cancellation_stays_silent_and_closes_on_worker(action):
    entered = Event();release = Event();closed = Event();threads = []
    def generate(text, config):
        threads.append(get_ident());entered.set()
        try:
            assert release.wait(3)
            yield SimpleNamespace(audio_float_array=np.ones(32))
        finally:
            threads.append(get_ident());closed.set()
    synth, _ = engine(generate)
    h = PrefetchHarness(synth, prefetch_enabled=True)
    try:
        h.runtime.prepare(PREFIX, **KEY);assert entered.wait(3)
        if action == 'cancel': h.runtime.prepare('', cancel=True, **KEY)
        if action == 'stop': h.runtime.control('future-playback', 'stop')
        if action == 'stop_all': h.runtime.stop_all()
        if action == 'close':
            # Release the simulated native call while close waits for its worker.
            from threading import Thread
            closer = Thread(target=h.runtime.close);closer.start()
            release.set();closer.join(3);assert not closer.is_alive()
        else:
            release.set()
        assert closed.wait(3)
        with h.runtime._condition:
            assert h.runtime._condition.wait_for(lambda: h.runtime._prefetch.job is None, 3)
        assert len(set(threads)) == 1
        assert h.events == [] and h.players.empty()
    finally:
        release.set();h.runtime.close()


def test_no_second_job_or_model_while_native_prepare_is_running():
    entered=Event();release=Event()
    def generate(text, _):
        entered.set();assert release.wait(3)
        yield SimpleNamespace(audio_float_array=np.ones(10))
    synth, _=engine(generate)
    h=PrefetchHarness(synth,prefetch_enabled=True)
    try:
        h.runtime.prepare(PREFIX, **KEY);assert entered.wait(3)
        newer={**KEY,'generation_id':'next','prepared_id':'next'}
        assert not h.runtime.prepare(PREFIX, **newer)
        assert h.runtime._prefetch.job.key == tuple(KEY.values())
        release.set()
        with h.runtime._condition:
            assert h.runtime._condition.wait_for(lambda:h.runtime._prefetch.job is None,3)
        assert h.events == []
    finally:
        release.set();h.runtime.close()


@pytest.mark.parametrize('same_generation', [True, False])
def test_final_arriving_during_native_prepare_reuses_only_its_own_job(same_generation):
    entered=Event();release=Event();calls=[]
    def generate(text, _):
        calls.append((text,get_ident()))
        if len(calls)==1:
            entered.set();assert release.wait(3)
        yield SimpleNamespace(audio_float_array=np.ones(10))
    synth,_=engine(generate);h=PrefetchHarness(synth,prefetch_enabled=True)
    try:
        h.runtime.prepare(PREFIX,**KEY);assert entered.wait(3)
        final_key=dict(KEY)
        if not same_generation:
            final_key.update(generation_id='replacement',prepared_id='replacement')
        identity=h.runtime.submit(FINAL,**final_key)
        assert h.players.empty() and h.events==[]
        release.set()
        player=h.active(identity);player.drain.set();h.wait(identity,'finished')
        assert len(calls)==(2 if same_generation else 3)
        assert len(player.audio)==2
        assert len({thread for _,thread in calls})==1
        assert h.runtime._prefetch.job is None
    finally:
        release.set();h.runtime.close()


def test_oversized_preparation_is_discarded_without_status_or_player():
    synth, _=engine(lambda *_:iter([SimpleNamespace(
        audio_float_array=np.zeros(MAX_PREPARED_BYTES//4+1,dtype=np.float32))]))
    h=PrefetchHarness(synth,prefetch_enabled=True)
    try:
        h.runtime.prepare(PREFIX,**KEY)
        with h.runtime._condition:
            assert h.runtime._condition.wait_for(lambda:h.runtime._prefetch.job is None,3)
        assert h.events == [] and h.players.empty()
    finally:
        h.runtime.close()


def test_cancel_before_prepare_and_bounded_retirement():
    slot=PrefetchSlot(lambda:0.)
    key=preparation_key(**KEY)
    slot.cancel(key)
    assert not slot.request(key,PREFIX)
    for i in range(300):slot.cancel(('r','g',str(i)))
    assert len(slot.retired)==256


def test_expiry_releases_immutable_cached_payload():
    now=[0.];slot=PrefetchSlot(lambda:now[0]);key=preparation_key(**KEY)
    slot.request(key,PREFIX);job=slot.start();slot.finish(job,object())
    now[0]=8.;slot.expire()
    assert slot.job is None and job.audio is None


def test_preparation_rejected_during_committed_playback():
    synth,_=engine();h=PrefetchHarness(synth,prefetch_enabled=True)
    try:
        identity=h.runtime.submit('이미 재생 중입니다.')
        player=h.active(identity)
        assert not h.runtime.prepare(PREFIX,**KEY)
        player.drain.set();h.wait(identity,'finished')
    finally:h.runtime.close()


def test_prepare_runs_on_single_worker_during_prerecorded_interim_drain_and_hits_final():
    """Prefetch overlaps with a draining prerecorded interim for the same turn."""
    class CooperativePlayer(Player):
        def begin_finish(self):
            self.finishing.set()

        def wait_finished(self, timeout=None):
            if not self.drain.wait(timeout):
                return False
            if self.fail:
                raise RuntimeError('device failed')
            return True

    class CooperativeHarness(PrefetchHarness):
        def make_player(self, **kwargs):
            player = CooperativePlayer(**kwargs)
            self.players.put(player)
            return player

    prerecorded_calls = []

    class FakePrerecorded:
        def generate(self, audio_id, cancel_event):
            prerecorded_calls.append(audio_id)
            yield np.ones(16, dtype=np.float32), 22050

    synth, calls = engine()
    h = CooperativeHarness(
        synth, prerecorded=FakePrerecorded(), prefetch_enabled=True,
    )
    try:
        interim_id = h.runtime.submit(
            '날씨를 확인할게요.', interim=True,
            request_id=KEY['request_id'],
            audio_id='function.get_weather.starting',
        )
        interim_player = h.active(interim_id)
        assert interim_player.finishing.wait(3)
        # Another turn cannot piggyback on this turn's prerecorded interim.
        assert not h.runtime.prepare(
            PREFIX, request_id='other-turn',
            generation_id='g', prepared_id='p',
        )
        # Same turn prepares segment 0 while the prerecorded interim drains.
        assert h.runtime.prepare(PREFIX, **KEY)
        ready(h)
        assert [text for text, _ in calls] == ['첫 문장입니다. ']
        # Final answer arrives before interim finishes draining and still hits prefetch.
        final_id = h.runtime.submit(FINAL, **KEY)
        interim_player.drain.set()
        h.wait(interim_id, 'finished')
        final_player = h.active(final_id)
        final_player.drain.set()
        h.wait(final_id, 'finished')
        assert prerecorded_calls == ['function.get_weather.starting']
        assert [text for text, _ in calls] == ['첫 문장입니다. ', '다음 문장입니다.']
        assert len({thread for _, thread in calls}) == 1
        assert len(final_player.audio) == 2
    finally:
        h.runtime.close()


@pytest.mark.parametrize('same_turn', [True, False])
def test_same_turn_interim_preserves_running_prefetch_for_final(same_turn):
    """A same-turn interim racing with in-flight prepare must not cancel it."""
    entered = Event()
    release = Event()
    calls = []

    def generate(text, _):
        calls.append((text, get_ident()))
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
        yield SimpleNamespace(audio_float_array=np.ones(16, dtype=np.float32))

    class FakePrerecorded:
        def generate(self, audio_id, cancel_event):
            yield np.ones(16, dtype=np.float32), 22050

    synth, _ = engine(generate)
    h = PrefetchHarness(
        synth, prerecorded=FakePrerecorded(), prefetch_enabled=True,
    )
    try:
        assert h.runtime.prepare(PREFIX, **KEY)
        assert entered.wait(3)
        interim_req = KEY['request_id'] if same_turn else 'other-turn'
        interim_id = h.runtime.submit(
            '조회할게요.', interim=True, request_id=interim_req,
            audio_id='function.get_homecam_events.starting',
        )
        final_id = h.runtime.submit(FINAL, **KEY)
        release.set()
        interim_player = h.active(interim_id)
        interim_player.drain.set()
        h.wait(interim_id, 'finished')
        final_player = h.active(final_id)
        final_player.drain.set()
        h.wait(final_id, 'finished')
        # Same turn reuses the prefetched first segment (2 synth calls total);
        # another turn's interim cancels it so final synthesizes both segments (3 calls).
        assert len(calls) == (2 if same_turn else 3)
        assert len({thread for _, thread in calls}) == 1
        assert len(final_player.audio) == 2
    finally:
        release.set()
        h.runtime.close()
