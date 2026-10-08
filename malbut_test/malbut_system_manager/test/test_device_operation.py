"""Manager validates the fixed backend boundary before creating a downstream Goal."""

from types import SimpleNamespace

import pytest

from malbut_system_manager.device_operation import (
    is_preparation, survives_runtime_stop, validate_device_operation,
)
from malbut_system_manager.mission_scheduler import is_movement


def _arguments(**changes):
    return {'request_id': 'voice:123', 'operation': 'status', 'arguments_json': '{}', **changes}


@pytest.mark.parametrize('operation', [
    'status', 'runtime_start', 'map_select', 'homecam_settings',
])
def test_registered_device_operations(operation):
    validate_device_operation(_arguments(operation=operation))


@pytest.mark.parametrize('changes', [
    {'request_id': ''}, {'request_id': 'x' * 129}, {'request_id': 'bad\nvalue'},
    {'operation': '/arbitrary/action'}, {'operation': 'https://example.com'},
    {'arguments_json': '[]'}, {'arguments_json': '{'},
    {'arguments_json': '{"x":NaN}'}, {'arguments_json': '{"x":Infinity}'},
    {'arguments_json': '{"x":"' + 'a' * 16384 + '"}'},
])
def test_invalid_device_operation_never_reaches_backend(changes):
    with pytest.raises(ValueError):
        validate_device_operation(_arguments(**changes))


@pytest.mark.parametrize('operation,movement', [
    ('runtime_start', True), ('map_select', True), ('runtime_stop', False), ('status', False),
])
def test_preparations_are_stop_targets_without_claiming_base(operation, movement):
    mission = SimpleNamespace(capability=SimpleNamespace(capability_id='device_operation'),
                              arguments={'operation': operation}, resources=frozenset())
    assert is_preparation(mission) is movement
    assert is_movement(mission) is movement
    assert survives_runtime_stop(mission) is not movement


@pytest.mark.parametrize('callback,handler', [
    ('_on_dispatch_timeout', 'handle_dispatch_timeout'),
    ('_on_cancel_rejected', 'handle_cancel_rejected'),
])
def test_unresolved_preparation_fences_late_dispatch(callback, handler):
    """A transport timeout cannot leave a late device start with a valid epoch."""
    from threading import RLock

    from malbut_system_manager.models import SchedulerEffects
    from malbut_system_manager.system_manager_node import SystemManagerNode

    mission = SimpleNamespace(
        generation=1, capability=SimpleNamespace(capability_id='device_operation'),
        arguments={'operation': 'runtime_start'})
    manager = object.__new__(SystemManagerNode)
    manager._lock = RLock()
    manager._effects_lock = RLock()
    manager._state = SimpleNamespace(get=lambda _: mission, movement_stopping=False)
    manager._stopped_mission_ids = set()
    manager._preparation_fenced_ids = set()
    manager._movement_epoch = 7
    manager._localization = None
    manager._scheduler = SimpleNamespace(**{handler: lambda *args: SchedulerEffects()})
    manager._apply_effects = lambda _: None
    manager._publish_state = lambda: None
    manager.get_logger = lambda: SimpleNamespace(error=lambda _: None, warning=lambda _: None)
    getattr(manager, callback)('preparation', 1, 'transport unresolved')
    assert manager._movement_epoch == 8
    assert manager._state.movement_stopping
    assert manager._stopped_mission_ids == {'preparation'}
    getattr(manager, callback)('preparation', 1, 'transport still unresolved')
    assert manager._movement_epoch == 8
