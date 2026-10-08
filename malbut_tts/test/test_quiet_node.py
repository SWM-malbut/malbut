"""Quiet service ACKs wait without blocking ROS status or renewal callbacks."""

from types import SimpleNamespace
from threading import Event

import pytest

from malbut_tts import node as tts_node
from test_node import FakeRuntime, fake_ros  # noqa: F401
from test_runtime import Harness


class QuietRuntime(FakeRuntime):
    def __init__(self, on_status):
        super().__init__(on_status)
        self.state = 'pending'
        self.leases = []
        self.cancelled = []

    def control_web_talk(self, lease_id, active, ttl_s):
        self.leases.append((lease_id, active, ttl_s))
        return bool(lease_id)

    def web_talk_status(self, lease_id):
        return self.state

    def cancel_request(self, request_id):
        self.cancelled.append(request_id)
        return bool(request_id)

    def request_is_quiescent(self, request_id):
        return request_id in self.cancelled and self.state == 'quiet'


def service(entities, suffix):
    return next(callback for _, name, callback in entities.services
                if name.endswith(suffix))


def request(callback, lease_id='web', active=True, ttl_s=10):
    return callback(SimpleNamespace(lease_id=lease_id, active=active, ttl_s=ttl_s),
                    SimpleNamespace(accepted=False))


def finished(coroutine):
    with pytest.raises(StopIteration) as result:
        coroutine.send(None)
    return result.value.value.accepted


def test_cancel_playback_request_service_fences_only_the_given_id(fake_ros):
    node = tts_node.create_tts_node(QuietRuntime)
    try:
        callback = service(fake_ros, '/cancel_playback_request')
        assert callback(SimpleNamespace(request_id='turn'),
                        SimpleNamespace(accepted=False)).accepted
        assert node._runtime.cancelled == ['turn']
    finally:
        node.destroy_node()


@pytest.mark.parametrize('quiet', [False, True])
def test_cancel_service_reports_current_quiescence_separately_from_acceptance(fake_ros, quiet):
    node = tts_node.create_tts_node(QuietRuntime)
    try:
        node._runtime.state = 'quiet' if quiet else 'pending'
        callback = service(fake_ros, '/cancel_playback_request')
        response = callback(SimpleNamespace(request_id='turn'),
                            SimpleNamespace(accepted=False, quiescent=False))
        assert response.accepted
        assert response.quiescent is quiet
        response = callback(SimpleNamespace(request_id=''),
                            SimpleNamespace(accepted=False, quiescent=True))
        assert not response.accepted
        assert not response.quiescent
    finally:
        node.destroy_node()


def test_quiet_service_waits_for_cleanup_while_other_callbacks_run(fake_ros):
    node = tts_node.create_tts_node(QuietRuntime)
    try:
        call = request(service(fake_ros, '/playback_web_talk_control'))
        future = call.send(None)
        assert not future.done()
        fake_ros.subscriptions[0][2](SimpleNamespace(
            text='late', request_type=0, interim=False))
        assert node._runtime.submitted == [('late', 0, False)]
        node._runtime.state = 'quiet'
        fake_ros.timers[0][1]()
        assert finished(call)
    finally:
        node.destroy_node()


@pytest.mark.parametrize('state', ['failed', 'rejected'])
def test_quiet_service_rejects_failed_or_expired_cleanup(fake_ros, state):
    node = tts_node.create_tts_node(QuietRuntime)
    try:
        node._runtime.state = state
        assert not finished(request(service(fake_ros, '/playback_web_talk_control')))
    finally:
        node.destroy_node()


def test_quiet_ack_deadline_is_not_extended_by_same_lease_renewal(fake_ros, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(tts_node, 'monotonic', lambda: now[0])
    node = tts_node.create_tts_node(QuietRuntime)
    try:
        callback = service(fake_ros, '/playback_web_talk_control')
        first = request(callback, ttl_s=2)
        first.send(None)
        now[0] = 1
        renewal = request(callback, ttl_s=10)
        renewal.send(None)
        now[0] = 2
        node._runtime.state = 'quiet'
        fake_ros.timers[0][1]()
        assert not finished(first)
        assert finished(renewal)
    finally:
        node.destroy_node()


def test_same_lease_renewal_invalidates_old_ack_before_its_deadline(fake_ros, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(tts_node, 'monotonic', lambda: now[0])
    node = tts_node.create_tts_node(QuietRuntime)
    try:
        callback = service(fake_ros, '/playback_web_talk_control')
        first = request(callback, ttl_s=10)
        first.send(None)
        now[0] = 1
        renewal = request(callback, ttl_s=2)
        renewal.send(None)
        node._runtime.state = 'quiet'
        fake_ros.timers[0][1]()
        assert not finished(first)
        assert finished(renewal)
    finally:
        node.destroy_node()


def test_already_resolved_ack_is_rechecked_after_renewal_before_response(fake_ros):
    node = tts_node.create_tts_node(QuietRuntime)
    try:
        callback = service(fake_ros, '/playback_web_talk_control')
        first = request(callback)
        first.send(None)
        node._runtime.state = 'quiet'
        fake_ros.timers[0][1]()
        # The timer resolved first, but its coroutine has not returned yet.
        assert finished(request(callback, ttl_s=1))
        assert not finished(first)
    finally:
        node.destroy_node()


@pytest.mark.parametrize('replacement', ['other', 'release'])
def test_stale_quiet_request_cannot_ack_after_replacement_or_release(fake_ros, replacement):
    node = tts_node.create_tts_node(QuietRuntime)
    try:
        callback = service(fake_ros, '/playback_web_talk_control')
        first = request(callback)
        first.send(None)
        second = request(callback, lease_id='other' if replacement == 'other' else 'web',
                         active=replacement != 'release')
        if replacement == 'release':
            assert finished(second)
        else:
            second.send(None)
        node._runtime.state = 'quiet'
        fake_ros.timers[0][1]()
        assert not finished(first)
        if replacement == 'other':
            assert finished(second)
    finally:
        node.destroy_node()


def test_shutdown_resolves_waiting_quiet_ack_as_rejected(fake_ros):
    node = tts_node.create_tts_node(QuietRuntime)
    call = request(service(fake_ros, '/playback_web_talk_control'))
    call.send(None)
    node.destroy_node()
    assert not finished(call)


def test_actual_runtime_cleanup_is_required_for_service_ack(fake_ros):
    h = Harness()
    release, closing = Event(), Event()
    node = tts_node.create_tts_node(lambda on_status: h.runtime)
    try:
        active = h.runtime.submit('active')
        player = h.active(active)

        def close():
            closing.set()
            assert release.wait(3)

        player.close = close
        call = request(service(fake_ros, '/playback_web_talk_control'))
        future = call.send(None)
        assert closing.wait(3)
        fake_ros.timers[0][1]()
        assert not future.done()
        late = h.runtime.submit('late response', request_id='late')
        h.wait(late, 'stopped')
        release.set()
        h.wait(active, 'stopped')
        with h.runtime._condition:
            assert h.runtime._condition.wait_for(
                lambda: h.runtime.web_talk_status('web') == 'quiet', 3)
        fake_ros.timers[0][1]()
        assert finished(call)
    finally:
        release.set()
        node.destroy_node()


@pytest.mark.parametrize('preceding', ['stop_all', 'cancel_request'])
def test_existing_control_before_web_service_keeps_executor_responsive(fake_ros, preceding):
    h = Harness()
    release, stopping = Event(), Event()
    node = tts_node.create_tts_node(lambda on_status: h.runtime)
    try:
        active = h.runtime.submit('active', request_id='turn')
        player = h.active(active)

        def stop():
            stopping.set()
            assert release.wait(3)
            player.drain.set()

        player.stop = stop
        if preceding == 'stop_all':
            callback = service(fake_ros, '/playback_control')
            response = callback(SimpleNamespace(playback_id='', command='stop_all'),
                                SimpleNamespace(accepted=False))
        else:
            callback = service(fake_ros, '/cancel_playback_request')
            response = callback(SimpleNamespace(request_id='turn'),
                                SimpleNamespace(accepted=False))
        assert response.accepted
        assert stopping.wait(3)
        call = request(service(fake_ros, '/playback_web_talk_control'))
        future = call.send(None)
        fake_ros.timers[0][1]()
        assert not future.done()
        release.set()
        h.wait(active, 'stopped')
        with h.runtime._condition:
            assert h.runtime._condition.wait_for(
                lambda: h.runtime.web_talk_status('web') == 'quiet', 3)
        fake_ros.timers[0][1]()
        assert finished(call)
    finally:
        release.set()
        node.destroy_node()
