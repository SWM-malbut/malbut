"""Request cancellation and web quiet admission are separate from STOP receipt."""

from queue import Queue
from threading import Event, Thread

import pytest

from test_runtime import Harness


@pytest.mark.parametrize('interim', [False, True])
def test_cancel_before_dds_receipt_suppresses_every_correlated_reply(interim):
    h = Harness()
    try:
        assert h.runtime.cancel_request('expired-turn')
        pid = h.runtime.submit('late answer', request_id='expired-turn', interim=interim)
        h.wait(pid, 'stopped')
        assert h.synth.texts == []
        assert h.request_ids == ['expired-turn']
        assert h.interim_flags == [interim]
    finally:
        h.runtime.close()


def test_cancel_request_stops_only_its_active_and_pending_audio():
    h = Harness()
    try:
        active = h.runtime.submit('active target', request_id='target', interim=True)
        player = h.active(active)
        pending = h.runtime.submit('pending target', request_id='target', interim=True)
        other = h.runtime.submit('unrelated answer', request_id='other')
        assert h.runtime.cancel_request('target')
        h.wait(active, 'stopped')
        h.wait(pending, 'stopped')
        assert player.cancel.is_set()
        h.active(other).drain.set()
        h.wait(other, 'finished')
        assert h.synth.texts == ['active target', 'unrelated answer']
    finally:
        h.runtime.close()


def test_pending_request_cancel_preserves_unrelated_active_and_confirmation():
    h = Harness()
    try:
        active = h.runtime.submit('other answer', request_id='other')
        player = h.active(active)
        pending = h.runtime.submit('target answer', request_id='target')
        assert h.runtime.cancel_request('target')
        h.wait(pending, 'stopped')
        assert not player.cancel.is_set()
        player.drain.set()
        h.wait(active, 'finished')
        confirmation = h.runtime.submit('confirmation', 2, request_id='target')
        h.active(confirmation).drain.set()
        h.wait(confirmation, 'finished')
    finally:
        h.runtime.close()


@pytest.mark.parametrize('request_id', ['', ' ', None, 1, 'x' * 257])
def test_cancel_request_rejects_invalid_ids(request_id):
    h = Harness()
    try:
        assert not h.runtime.cancel_request(request_id)
    finally:
        h.runtime.close()


def test_request_cancellation_tombstones_are_bounded():
    h = Harness()
    try:
        for index in range(270):
            assert h.runtime.cancel_request(str(index))
        assert len(h.runtime._cancelled_request_ids) == 256
        assert '0' not in h.runtime._cancelled_request_ids
        assert '269' in h.runtime._cancelled_request_ids
    finally:
        h.runtime.close()


def test_request_quiescence_confirms_cleanup_even_when_terminal_status_is_lost():
    h = Harness()
    release, closing = Event(), Event()
    try:
        active = h.runtime.submit('active', request_id='turn')
        player = h.active(active)

        def close():
            closing.set()
            assert release.wait(3)

        player.close = close
        h.runtime._on_status = lambda *args: None
        assert h.runtime.cancel_request('turn')
        assert closing.wait(3)
        assert not h.runtime.request_is_quiescent('turn')
        release.set()
        with h.runtime._condition:
            assert h.runtime._condition.wait_for(
                lambda: h.runtime._active is None, 3)
        assert h.runtime.request_is_quiescent('turn')
        assert (active, 'stopped') not in h.events
    finally:
        release.set()
        h.runtime.close()


def test_request_quiescence_does_not_claim_unseen_ids_or_stop_unrelated_audio():
    h = Harness()
    try:
        other = h.runtime.submit('other', request_id='other')
        player = h.active(other)
        assert not h.runtime.request_is_quiescent('other')
        assert not h.runtime.request_is_quiescent('unseen')
        assert h.runtime.cancel_request('turn')
        assert h.runtime.request_is_quiescent('turn')
        assert not player.cancel.is_set()
        assert not h.runtime.request_is_quiescent('other')
        player.drain.set()
        h.wait(other, 'finished')
    finally:
        h.runtime.close()


def test_request_quiescence_rejects_outstanding_stop_and_failed_cleanup():
    h = Harness()
    stopping, release = Event(), Event()
    try:
        active = h.runtime.submit('active', request_id='turn')
        player = h.active(active)

        def stop():
            stopping.set()
            assert release.wait(3)
            player.drain.set()
            raise RuntimeError('fake stop failure')

        player.stop = stop
        assert h.runtime.cancel_request('turn')
        assert stopping.wait(3)
        assert not h.runtime.request_is_quiescent('turn')
        release.set()
        h.wait(active, 'failed')
        assert not h.runtime.request_is_quiescent('turn')
    finally:
        release.set()
        h.runtime.close()


def test_cancelled_active_request_remains_fenced_when_history_rotates():
    h = Harness()
    release_close, closing = Event(), Event()
    try:
        active = h.runtime.submit('active target', request_id='target')
        player = h.active(active)

        def close():
            closing.set()
            assert release_close.wait(3)

        player.close = close
        assert h.runtime.cancel_request('target')
        assert closing.wait(3)
        for index in range(270):
            assert h.runtime.cancel_request(str(index))
        assert len(h.runtime._cancelled_request_ids) == 256
        assert 'target' in h.runtime._cancelled_request_ids
        late = h.runtime.submit('late target', request_id='target')
        h.wait(late, 'stopped')
        assert h.synth.texts == ['active target']
    finally:
        release_close.set()
        h.runtime.close()


def test_web_quiet_waits_for_actual_cleanup_and_rejects_new_requests():
    h = Harness()
    release_close, closing = Event(), Event()
    original_factory = h.runtime._player_factory

    def factory(**kwargs):
        player = original_factory(**kwargs)

        def close():
            closing.set()
            assert release_close.wait(3)

        player.close = close
        return player

    h.runtime._player_factory = factory
    try:
        active = h.runtime.submit('active', request_id='active-request')
        h.active(active)
        pending = h.runtime.submit('waiting', request_id='waiting-request')
        assert h.runtime.control_web_talk('web', True, 10)
        assert closing.wait(3)
        assert h.runtime.web_talk_status('web') == 'pending'
        h.wait(pending, 'stopped')
        late = h.runtime.submit('late DDS', request_id='late-request')
        h.wait(late, 'stopped')
        assert h.request_ids[-1] == 'late-request'
        release_close.set()
        h.wait(active, 'stopped')
        with h.runtime._condition:
            assert h.runtime._condition.wait_for(
                lambda: h.runtime.web_talk_status('web') == 'quiet', 3)
        assert h.synth.texts == ['active']
    finally:
        release_close.set()
        h.runtime.close()


def test_web_quiet_cancels_before_player_factory_returns():
    h = Harness()
    entered, release = Event(), Event()
    original_factory = h.runtime._player_factory

    def factory(**kwargs):
        entered.set()
        assert release.wait(3)
        return original_factory(**kwargs)

    h.runtime._player_factory = factory
    try:
        active = h.runtime.submit('not synthesized', request_id='turn')
        assert entered.wait(3)
        assert h.runtime.control_web_talk('web', True, 10)
        assert h.runtime.web_talk_status('web') == 'pending'
        release.set()
        h.wait(active, 'stopped')
        with h.runtime._condition:
            assert h.runtime._condition.wait_for(
                lambda: h.runtime.web_talk_status('web') == 'quiet', 3)
        assert h.synth.texts == []
        assert h.players.get(timeout=3).closed.is_set()
    finally:
        release.set()
        h.runtime.close()


def test_web_quiet_stop_cannot_block_service_admission_or_claim_quiescence():
    h = Harness()
    stopped, release = Event(), Event()
    try:
        active = h.runtime.submit('active')
        player = h.active(active)

        def stop():
            stopped.set()
            assert release.wait(3)
            player.drain.set()

        player.stop = stop
        assert h.runtime.control_web_talk('web', True, 10)
        assert stopped.wait(3)
        assert h.runtime.web_talk_status('web') == 'pending'
        assert h.runtime.control_web_talk('web', True, 10)
        assert h.runtime.control_web_talk('web', False, 0)
        assert h.runtime.web_talk_status('web') == 'rejected'
        release.set()
        h.wait(active, 'stopped')
    finally:
        release.set()
        h.runtime.close()


@pytest.mark.parametrize('command', ['stop', 'stop_all', 'confirmation'])
def test_legacy_stop_and_preemption_do_not_block_later_web_quiet(command):
    h = Harness()
    stopped, release = Event(), Event()
    returned = Queue()
    caller = None
    try:
        active = h.runtime.submit('active')
        player = h.active(active)

        def stop():
            stopped.set()
            assert release.wait(3)
            player.drain.set()

        player.stop = stop

        def legacy_callback():
            if command == 'confirmation':
                returned.put(h.runtime.submit('question', 2))
            else:
                returned.put(h.runtime.control(active, command))

        caller = Thread(target=legacy_callback)
        caller.start()
        assert stopped.wait(3)
        assert returned.get(timeout=0.5)
        assert h.runtime.control_web_talk('web', True, 10)
        assert h.runtime.web_talk_status('web') == 'pending'
        release.set()
        h.wait(active, 'stopped')
    finally:
        release.set()
        if caller is not None:
            caller.join(3)
        h.runtime.close()


@pytest.mark.parametrize('failure', ['stop', 'close'])
def test_web_quiet_does_not_acknowledge_failed_device_cleanup(failure):
    h = Harness()
    try:
        pid = h.runtime.submit('active')
        player = h.active(pid)

        def fail():
            player.drain.set()
            raise RuntimeError('fake device failure')

        setattr(player, failure, fail)
        assert h.runtime.control_web_talk('web', True, 10)
        h.wait(pid, 'failed')
        assert h.runtime.web_talk_status('web') == 'failed'
        assert h.runtime.control_web_talk('replacement', True, 10)
        assert h.runtime.web_talk_status('replacement') == 'failed'
    finally:
        h.runtime.close()


def test_web_lease_renewal_replacement_stale_release_and_expiry():
    now = [0.0]
    h = Harness(clock=lambda: now[0])
    try:
        assert h.runtime.control_web_talk('first', True, 2)
        now[0] = 1
        assert h.runtime.control_web_talk('first', True, 3)
        now[0] = 2.1
        assert h.runtime.web_talk_status('first') == 'quiet'
        assert h.runtime.control_web_talk('second', True, 2)
        assert h.runtime.web_talk_status('first') == 'rejected'
        assert not h.runtime.control_web_talk('first', False, 0)
        assert h.runtime.web_talk_status('second') == 'quiet'
        now[0] = 4.1
        assert h.runtime.web_talk_status('second') == 'rejected'
        assert not h.runtime.control_web_talk('second', False, 0)
        pid = h.runtime.submit('after expiry')
        h.active(pid).drain.set()
        h.wait(pid, 'finished')
    finally:
        h.runtime.close()


@pytest.mark.parametrize('lease_id,ttl', [
    ('', 1), (' ', 1), ('x' * 201, 1), ('ok', 0), ('ok', -1),
    ('ok', 16), ('ok', float('nan')), ('ok', float('inf')), ('ok', True),
])
def test_web_quiet_rejects_invalid_lease(lease_id, ttl):
    h = Harness()
    try:
        assert not h.runtime.control_web_talk(lease_id, True, ttl)
    finally:
        h.runtime.close()
