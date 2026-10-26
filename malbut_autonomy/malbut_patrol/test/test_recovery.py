"""Check bounded history and motion ordering for patrol escape manoeuvres."""

import math
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from nav2_msgs.action import ComputePathToPose, FollowPath
from nav_msgs.msg import Path
from geometry_msgs.msg import PoseStamped

from malbut_patrol.patrol_manager import PatrolInterrupted, PatrolManager
from malbut_patrol.recovery import BreadcrumbTrail, path_segments


def test_history_retains_slow_progress_and_expires():
    trail = BreadcrumbTrail()
    for index in range(101):
        trail.record((index*.01, 0., 0.), (0., 0., 0.), index*.1)
    points = trail.retreat((1., 0., 0.), 10.)
    assert len(points) > 10 and points[-1][0] == 0.
    assert not trail.retreat((1., 0., 0.), 12.)


@pytest.mark.parametrize('anchor', [(.2, 0., 0.), (0., 0., .2)])
def test_localization_change_invalidates_old_travel(anchor):
    trail = BreadcrumbTrail()
    for index in range(10):
        trail.record((index*.1, 0., 0.), (0., 0., 0.), index*.1)
    trail.record((.9, 0., 0.), anchor, 1.)
    assert not trail.retreat((.9, 0., 0.), 1.)


def test_shunts_are_split_without_skipping_cusp():
    points = [(x, 0., 0.) for x in (1., .9, .8, .9, 1., .9, .8, .7, .6)]
    trail = [(x/10, 0., 0.) for x in range(10, 5, -1)]
    segments = path_segments(points, trail)
    assert [direction for direction, _ in segments] == [-1, 1, -1]
    assert [indices for _, indices in segments] == [[0, 1, 2], [2, 3, 4], [4, 5, 6, 7, 8]]
    with pytest.raises(ValueError, match='budget'):
        path_segments(points, trail, maximum_switches=1)


@pytest.mark.parametrize('points', [
    [(1., 0., 0.), (.5, 0., 0.)],  # Discontinuous shortcut.
    [(1., 0., 0.), (.9, 0., math.nan)],
    [(1., 0., math.pi/2), (.9, 0., math.pi/2)],  # Sideways travel.
    [(1., 0., math.pi), (.9, 0., math.pi)],  # No reversing.
])
def test_invalid_escape_paths_do_not_produce_motion(points):
    with pytest.raises(ValueError):
        path_segments(points, [points[0], points[-1]])


def recovery_probe():
    manager = object.__new__(PatrolManager)
    manager.settings = {'recovery_enabled': True}
    manager.recovery_status = {'attempts': 0, 'state': 'not_started'}
    manager.trail = Mock()
    trail = [(x/10, 0., 0.) for x in range(10, 3, -1)]
    manager.trail.retreat.return_value = trail
    manager.recovery_scan_received = time.monotonic()
    manager.recovery_planner = Mock()
    manager.recovery_controller = Mock()
    manager.map_message = SimpleNamespace(header=SimpleNamespace(frame_id='map'))
    manager.get_clock = Mock()
    clock = manager.get_clock.return_value.now.return_value
    clock.to_msg.return_value = PoseStamped().header.stamp
    manager._robot_pose = Mock(return_value=(1., 0., 0.))
    manager._robot_xy = Mock(return_value=(.4, 0.))
    manager._sample_trail = Mock()
    manager._settle_child = Mock(return_value=True)
    manager._recovery_stop = Mock()
    path = Path()
    path.header.frame_id = 'map'
    for x in (1., .9, .8, .9, 1., .9, .8, .7, .6, .5, .4):
        pose = PoseStamped()
        pose.header.frame_id = 'map'
        pose.pose.position.x = x
        pose.pose.orientation.w = 1.
        path.poses.append(pose)
    manager.child = SimpleNamespace(result=SimpleNamespace(result=SimpleNamespace(path=path)))
    return manager


def test_recovery_waits_for_old_goal_and_stops_between_directions():
    manager = recovery_probe()
    events = []
    manager._settle_child.side_effect = lambda: events.append('cancel_confirmed') or True
    manager._recovery_stop.side_effect = lambda _: events.append('stopped')

    def run(client, request, handle, timeout, **kwargs):
        events.append('plan' if isinstance(request, ComputePathToPose.Goal) else manager.phase)
        if isinstance(request, FollowPath.Goal):
            assert request.controller_id == 'PatrolRecovery'
        return True

    manager._run_child = run
    assert manager._recover(Mock())
    assert events == ['cancel_confirmed', 'stopped', 'plan', 'stopped',
                      'RECOVERY_REVERSE', 'stopped', 'RECOVERY_FORWARD', 'stopped',
                      'RECOVERY_REVERSE', 'stopped']
    assert manager.recovery_status['direction_changes'] == 2
    assert not manager._recovering


def test_unconfirmed_cancel_never_starts_escape():
    manager = recovery_probe()
    manager._settle_child.return_value = False
    manager._run_child = Mock()
    with pytest.raises(PatrolInterrupted, match='unconfirmed'):
        manager._recover(Mock())
    manager._run_child.assert_not_called()


def test_failed_reverse_stops_without_the_next_forward_leg():
    manager = recovery_probe()
    manager._run_child = Mock(side_effect=[True, False])
    assert not manager._recover(Mock())
    assert manager._run_child.call_count == 2
    assert manager.recovery_status['state'] == 'blocked'
    manager.recovery_status['attempts'] = 2
    assert not manager._recover(Mock())
    assert manager._run_child.call_count == 2


@pytest.mark.parametrize('cell', [100, 99, -1])
def test_history_fallback_rejects_new_block_or_unknown_cell(cell):
    from nav_msgs.msg import OccupancyGrid
    from threading import RLock

    manager = recovery_probe()
    manager.lock = RLock()
    grid = OccupancyGrid()
    grid.info.width, grid.info.height, grid.info.resolution = 40, 20, .05
    grid.info.origin.position.y = -.5
    grid.info.origin.orientation.w = 1.
    grid.data = [0]*800
    manager.costmap = grid
    trail = manager.trail.retreat.return_value
    assert manager._history_path(trail) is not None
    grid.data[10*40+14] = cell
    assert manager._history_path(trail) is None


def test_cancel_during_shunt_never_starts_remaining_segments():
    manager = recovery_probe()
    manager._run_child = Mock(side_effect=[True, PatrolInterrupted('Patrol canceled')])
    with pytest.raises(PatrolInterrupted, match='canceled'):
        manager._recover(Mock())
    assert manager._run_child.call_count == 2
    assert not manager._recovering
    assert manager.recovery_status['state'] == 'interrupted'


@pytest.mark.parametrize('failure', ['tf', 'localization', 'scan', 'costmap', 'deadline'])
def test_recovery_interrupts_when_live_safety_inputs_expire(failure):
    manager = recovery_probe()
    manager.settings.update(base_frame='base_footprint', odom_frame='odom')
    manager._recovering = True
    now = time.monotonic()
    manager._recovery_deadline = now + 90.
    manager.costmap_received = now
    manager.recovery_scan_received = now
    manager.trail.consistent.return_value = failure != 'localization'
    manager.get_clock.return_value.now.return_value.nanoseconds = 10_000_000_000
    transform = SimpleNamespace(
        header=SimpleNamespace(stamp=SimpleNamespace(sec=10, nanosec=0)),
        transform=SimpleNamespace(translation=SimpleNamespace(x=0., y=0.),
                                  rotation=SimpleNamespace(x=0., y=0., z=0., w=1.)))
    manager.tf = Mock()
    manager.tf.lookup_transform.return_value = transform
    if failure == 'tf':
        transform.header.stamp.sec = 9
    elif failure == 'scan':
        manager.recovery_scan_received -= 1.
    elif failure == 'costmap':
        manager.costmap_received -= 3.
    elif failure == 'deadline':
        manager._recovery_deadline = now - 1.
    with pytest.raises(PatrolInterrupted):
        PatrolManager._sample_trail(manager)
    manager.trail.clear.assert_called_once()


def test_navigation_retries_once_after_escape_and_owns_recovery_tree():
    from nav2_msgs.action import NavigateToPose
    from pathlib import Path
    from xml.etree import ElementTree as ET

    manager = recovery_probe()
    tree = Path(__file__).parents[1] / 'config/patrol_navigation.xml'
    manager.settings.update(navigation_timeout_s=120., navigation_behavior_tree=str(tree))
    manager.phase = 'NAVIGATING'
    manager.navigation = Mock()
    manager._run_child = Mock(side_effect=[False, True])
    manager._recover = Mock(return_value=True)
    request = NavigateToPose.Goal()
    assert manager._navigate(request, Mock())
    assert request.behavior_tree == str(tree)
    assert manager._run_child.call_count == 2
    manager._recover.assert_called_once()
    tags = {node.tag for node in ET.parse(tree).iter()}
    assert 'ComputePathToPose' in tags and 'FollowPath' in tags
    assert not tags & {'Spin', 'BackUp', 'Wait'}
