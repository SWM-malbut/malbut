"""Keep map-provider ownership outside the generic system manager."""

from pathlib import Path

from malbut_system_manager.state_store import StateStore
from malbut_system_manager.system_manager_node import SystemManagerNode


def test_manager_has_no_map_modes_or_backend_process_owner():
    """Removing map gates must not introduce another runtime state machine."""
    assert not hasattr(StateStore(), 'localization')
    assert not hasattr(SystemManagerNode, '_create_localization')
    source = (Path(__file__).parents[1]
              / 'malbut_system_manager/system_manager_node.py').read_text()
    assert 'SlamProcess' not in source
    assert 'localization_control' not in source
    assert 'LocalizationMode' not in source
