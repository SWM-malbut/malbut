"""Check SLAM ownership around the existing exploration Action without DDS."""

from concurrent.futures import Future
from threading import Event, Lock, Thread
from types import SimpleNamespace
from unittest.mock import Mock

from malbut_autoslam.autoslam_node import AutoSlamNode, Interrupted


def _node(events, *, cancel=False, failure=False, cleanup_failure=False):
    handle = SimpleNamespace(
        is_cancel_requested=cancel,
        canceled=lambda: events.append('canceled'),
        succeed=lambda: events.append('succeeded'),
        abort=lambda: events.append('aborted'))
    node = SimpleNamespace(mapping_clients={'start': object(), 'stop': object()},
                           lock=Lock(), stopping=Event(), busy=True, save_uncertain=False,
                           _check=lambda handle: None, get_logger=Mock())

    def mapping(operation, handle):
        events.append(operation)
        if operation == 'start':
            node.mapping_requested = True
        if operation == 'stop' and cleanup_failure:
            raise RuntimeError('SLAM stop failed')

    def explore(handle, result):
        events.append('explore')
        if failure:
            raise Interrupted('canceled')
        result.success = True

    node._mapping_call = mapping
    node._explore = explore
    node._settle_child = lambda handle: events.append('child_terminal')
    return node, handle


def test_success_stops_slam_only_after_nav2_terminal_result():
    events = []
    node, handle = _node(events)
    result = AutoSlamNode._execute(node, handle)
    assert result.success
    assert events == ['start', 'explore', 'child_terminal', 'stop', 'succeeded']
    assert not node.busy


def test_cancel_and_exploration_error_still_stop_slam_before_returning():
    events = []
    node, handle = _node(events, cancel=True, failure=True)
    result = AutoSlamNode._execute(node, handle)
    assert not result.success
    assert events == ['start', 'explore', 'child_terminal', 'stop', 'canceled']


def test_cleanup_error_is_not_reported_as_a_successful_run():
    events = []
    node, handle = _node(events, cleanup_failure=True)
    result = AutoSlamNode._execute(node, handle)
    assert not result.success and node.save_uncertain
    assert 'SLAM stop failed' in result.message
    assert events[-1] == 'aborted'


def test_standalone_action_keeps_externally_managed_slam_untouched():
    events = []
    node, handle = _node(events)
    node.mapping_clients = {}
    assert AutoSlamNode._execute(node, handle).success
    assert events == ['explore', 'child_terminal', 'succeeded']


def test_unavailable_start_service_does_not_unload_an_existing_saved_map():
    events = []
    node, handle = _node(events)
    client = Mock()
    client.wait_for_service.return_value = False
    node.mapping_clients = {'start': client, 'stop': Mock()}
    node.settings = {'ready_timeout_s': 1.0}
    node._mapping_call = lambda operation, handle: AutoSlamNode._mapping_call(
        node, operation, handle)
    result = AutoSlamNode._execute(node, handle)
    assert not result.success and not node.mapping_requested
    assert events == ['child_terminal', 'aborted']
    node.mapping_clients['stop'].wait_for_service.assert_not_called()


def test_cancel_during_mapping_start_waits_for_the_non_cancellable_service():
    pending = Future()
    called = Event()
    client = Mock()
    client.wait_for_service.return_value = True
    client.call_async.side_effect = lambda request: called.set() or pending
    node = SimpleNamespace(mapping_clients={'start': client}, stopping=Event(),
                           settings={'ready_timeout_s': 1.0}, _feedback=Mock())
    handle = SimpleNamespace(is_cancel_requested=True)
    errors = []

    def start():
        try:
            AutoSlamNode._mapping_call(node, 'start', handle)
        except Exception as error:
            errors.append(error)

    worker = Thread(target=start, daemon=True)
    worker.start()
    assert called.wait(1.0)
    assert worker.is_alive()
    pending.set_result(SimpleNamespace(success=True, message='mapping started'))
    worker.join(1.0)
    assert not worker.is_alive() and not errors
    client.remove_pending_request.assert_not_called()
