"""Keep preparation cancellation truthful until Manager-owned localization ends."""

import time

from action_msgs.msg import GoalStatus
import pytest
import yaml

from test_action_integration import _wait_future, _wait_until
from test_resident_manager import resident, _send  # noqa: F401


class _Localization:
    stop_pending = False
    movement_identity = ''

    def stop_movement(self):
        self.stop_pending = True

    def close(self):
        pass


@pytest.mark.parametrize('timeout', [False, True])
def test_preparation_cancel_waits_for_internal_localization(resident, timeout):  # noqa: F811
    manager = resident.manager
    handle = _send(resident, 'device_operation', {
        'request_id': 'cancel-prep', 'operation': 'runtime_start', 'arguments_json': '{}',
    })
    _wait_until(lambda: resident.operations == ['runtime_start'])
    localization = _Localization()
    manager._localization = localization
    if timeout:
        manager._stop_timeout_s = 0.15
    original_epoch = manager._movement_epoch
    assert _wait_future(handle.cancel_goal_async()).goals_canceling
    _wait_until(lambda: bool(manager._preparation_terminals))
    result = handle.get_result_async()
    if timeout:
        terminal = _wait_future(result)
        assert terminal.status == GoalStatus.STATUS_ABORTED
        assert yaml.safe_load(terminal.result.result_yaml)['code'] == 'stop_unconfirmed'
        assert manager._state.movement_stopping
    else:
        time.sleep(0.1)
        assert not result.done()
        localization.stop_pending = False
        assert _wait_future(result).status == GoalStatus.STATUS_CANCELED
    localization.stop_pending = False
    assert manager._movement_epoch == original_epoch + 1
