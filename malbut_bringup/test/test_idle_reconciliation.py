"""
Verify idle retry policy using unchanged callback bodies from both copies.

These offline tests replace ROS transports, not lifecycle/reconciliation logic.
The ROS-backed RuntimeControl tests cover generated messages and callbacks.
"""

import ast
from concurrent.futures import Future
import json
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


REPOSITORY = Path(__file__).resolve().parents[2]


@pytest.fixture(params=('malbut_bringup', 'malbut_test/malbut_bringup'))
def runtime(request):
    source = REPOSITORY / request.param / 'malbut_bringup/runtime_control.py'
    tree = ast.parse(source.read_text())
    constants = {'PROFILES', 'PROCESSING_GROUPS'}
    selected = [node for node in tree.body if (
        isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in constants for target in node.targets)
        or isinstance(node, ast.FunctionDef) and node.name == 'required_groups'
        or isinstance(node, ast.ClassDef) and node.name in ('ProcessingRejected', 'RuntimeControl')
    )]
    selected[-1].bases = []
    now = [0.0]
    lifecycle = SimpleNamespace(Request=SimpleNamespace(PAUSE=1, RESUME=2))
    namespace = {
        'json': json, 'time': SimpleNamespace(monotonic=lambda: now[0]),
        'SetBool': SimpleNamespace(Request=SimpleNamespace),
        'ManageLifecycleNodes': lifecycle, 'String': SimpleNamespace,
    }
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), 'exec'), namespace)
    node = object.__new__(namespace['RuntimeControl'])
    node._lock = Lock()
    node._data_lock = Lock()
    node.timeout = 1.0
    node._report_lock = Lock()
    node._pending_reports = {}
    node._capabilities = set()
    node._have_state = True
    node._synced_demand = (frozenset({'patrol'}), False)
    node._sync_retry_at = None
    node._verify_requested = False
    node._ready_groups = set()
    node._group_errors = {}
    node._initialized = True
    node._map_initialized = True
    node._uncertain = False
    node._pending_command = None
    node._pending_restore = None
    node._closing = False
    node._mapping = False
    node._mapping_ready = False
    node._recovery_pending = False
    node._localization_active = True
    node._active = {'motion': True, 'planning': True}
    node._managers = {'motion': 'motion', 'planning': 'planning'}
    node._processing_active = {
        service: service == '/patrol_manager/set_processing'
        for services in namespace['PROCESSING_GROUPS'].values() for service in services}
    node._processing_pending = {}
    node._call = Mock(return_value=SimpleNamespace(success=True))

    def processing_reply(service, request):
        future = Future()
        future.set_result(node._call(service, request, service))
        return future

    node._processing_clients = {}
    for name in node._processing_active:
        client = Mock()
        client.call_async.side_effect = lambda request, name=name: processing_reply(name, request)
        node._processing_clients[name] = client
    node._manage = Mock()
    node._group_active = Mock(side_effect=lambda group: node._active[group])
    node._wait_map_tf = Mock()
    node._resolve_preparations = Mock()
    node._refresh_map_odom = Mock()
    node._demand_changed = Mock()
    node._usage = Mock()
    node.get_logger = Mock()
    return node, now, namespace


def _usage(node):
    return json.loads(node._usage.publish.call_args.args[0].data)


def test_confirmed_busy_refusal_retries_without_another_demand_event(runtime):
    node, now, _ = runtime
    node._call.return_value = SimpleNamespace(success=False, message='patrol is still running')
    node._on_demand_changed()
    assert not _usage(node)['ready']
    assert node._sync_retry_at == 1.0
    node._manage.assert_not_called()
    # Repeated empty manager state does not deliver a new demand event.
    node._state(SimpleNamespace(active_foreground_missions=[], active_background_missions=[]))
    node._demand_changed.trigger.assert_not_called()
    now[0] = 0.5
    node._tick()
    assert node._call.call_count == 1
    now[0] = 1.0
    node._tick()  # Still busy: bounded retry, never pause underneath the child.
    assert node._call.call_count == 2 and node._sync_retry_at == 2.0
    node._manage.assert_not_called()
    node._call.return_value = SimpleNamespace(success=True)
    now[0] = 2.0
    node._tick()
    assert node._call.call_count == 3
    assert [call.args for call in node._manage.call_args_list] == [('planning', 1), ('motion', 1)]
    assert node._synced_demand == (frozenset(), False)
    assert node._sync_retry_at is None
    assert _usage(node)['ready'] and _usage(node)['error'] == ''
    assert node._localization_active
    for tick in (3.0, 4.0, 5.0):
        now[0] = tick
        node._tick()
    assert node._call.call_count == 3 and node._manage.call_count == 2


def test_retry_uses_latest_demand_without_restarting_the_failed_patrol(runtime):
    node, now, _ = runtime
    node._call.return_value = SimpleNamespace(success=False, message='patrol is still running')
    node._on_demand_changed()
    node._state(SimpleNamespace(
        active_foreground_missions=[SimpleNamespace(capability_id='manual_drive')],
        active_background_missions=[]))
    node._call.return_value = SimpleNamespace(success=True)
    now[0] = 1.0
    node._tick()
    node._manage.assert_called_once_with('planning', 1)
    assert node._active == {'motion': True, 'planning': False}
    assert node._synced_demand == (frozenset({'manual_drive'}), False)
    assert node._sync_retry_at is None


@pytest.mark.parametrize('unconfirmed', [False, True])
def test_unknown_rpc_or_lifecycle_failure_is_not_retried(runtime, unconfirmed):
    node, now, _ = runtime

    def fail(*_):
        # Native _manage marks rejected/partial lifecycle changes uncertain;
        # an ambiguous processing dispatch is held by the fake transport here.
        node._uncertain = True
        raise RuntimeError('response unconfirmed' if unconfirmed else 'lifecycle rejected')

    if unconfirmed:
        node._call.side_effect = fail
    else:
        node._manage.side_effect = fail
    node._on_demand_changed()
    assert not _usage(node)['ready']
    assert node._sync_retry_at is None
    calls = node._call.call_count, node._manage.call_count
    for tick in (1.0, 2.0, 3.0):
        now[0] = tick
        node._tick()
    assert (node._call.call_count, node._manage.call_count) == calls


def test_startup_processing_refusal_also_schedules_reconciliation(runtime):
    node, now, _ = runtime
    node._initialized = False
    node._synced_demand = None
    node._node_states = {}
    node._configured = set()
    node._capabilities = {'patrol'}
    node._processing_active['/patrol_manager/set_processing'] = False
    node._call.return_value = SimpleNamespace(success=False, message='camera not ready')
    node._tick()
    assert node._initialized and node._sync_retry_at == 1.0
    assert not _usage(node)['ready']
    node._call.return_value = SimpleNamespace(success=True)
    now[0] = 1.0
    node._tick()
    assert _usage(node)['ready'] and node._sync_retry_at is None


def test_demand_event_clears_retry_after_processing_recovers(runtime):
    node, _, _ = runtime
    node._call.return_value = SimpleNamespace(success=False, message='patrol is still running')
    node._on_demand_changed()
    node._call.return_value = SimpleNamespace(success=True)
    node._on_demand_changed()
    assert node._sync_retry_at is None and _usage(node)['ready']


def test_unconfirmed_response_during_retry_is_not_repeated(runtime):
    node, now, _ = runtime
    node._call.return_value = SimpleNamespace(success=False, message='patrol is still running')
    node._on_demand_changed()

    def lose_reply(*_):
        node._uncertain = True
        raise RuntimeError('response unconfirmed')

    node._call.side_effect = lose_reply
    now[0] = 1.0
    node._tick()
    assert node._sync_retry_at is None and not _usage(node)['ready']
    now[0] = 2.0
    node._tick()
    assert node._call.call_count == 2


def test_direct_map_sync_failure_supersedes_an_old_processing_retry(runtime):
    node, now, _ = runtime
    node._call.return_value = SimpleNamespace(success=False, message='patrol is still running')
    node._on_demand_changed()
    node._call.return_value = SimpleNamespace(success=True)

    def rejected(*_):
        node._uncertain = True
        raise RuntimeError('lifecycle rejected')

    node._manage.side_effect = rejected
    with pytest.raises(RuntimeError, match='lifecycle rejected'):
        node._sync()  # Map RPCs use this same direct entry point.
    assert node._sync_retry_at is None
    now[0] = 1.0
    node._tick()
    assert node._manage.call_count == 1
