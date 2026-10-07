"""Unit tests for localization switching without Nav2 or slam_toolbox."""

import json
from threading import RLock, Thread
import time
from types import SimpleNamespace

from action_msgs.msg import GoalStatus
from builtin_interfaces.msg import Time
from malbut_interfaces.srv import PrepareLocalization
from nav2_msgs.srv import LoadMap, ManageLifecycleNodes, SetInitialPose
import pytest

from malbut_system_manager.localization import LocalizationController
from malbut_system_manager.models import ExecutionResource, LocalizationMode
from malbut_system_manager.system_manager_node import SystemManagerNode


class _Slam:
    def __init__(self, events):
        self.events = events
        self.alive = False

    def start(self):
        self.events.append('slam_start')
        self.alive = True

    def stop(self):
        self.events.append('slam_stop')
        self.alive = False


class _Node:
    def __init__(self):
        self.published = []

    def create_publisher(self, *args):
        if args[1].endswith('/status'):
            return SimpleNamespace(publish=lambda _: None)
        return SimpleNamespace(publish=self.published.append)

    def create_client(self, *args, **kwargs):
        return None

    def create_service(self, *args, **kwargs):
        return None

    def create_timer(self, *args, **kwargs):
        return SimpleNamespace(cancel=lambda: None)

    def get_logger(self):
        return SimpleNamespace(error=lambda _: None, warning=lambda _: None)

    def get_clock(self):
        return SimpleNamespace(now=lambda: SimpleNamespace(to_msg=lambda: Time()))


def _controller(monkeypatch, *, busy=False, load_result=LoadMap.Response.RESULT_SUCCESS,
                default_map='', can_mapping=None):
    events, modes = [], []
    node = _Node()
    controller = LocalizationController(
        node, None, slam=_Slam(events), on_mode=modes.append,
        can_switch=lambda: not busy, lifecycle_service='/manage',
        map_server_load_service='/load', service_timeout_s=1.0, default_map=default_map,
        can_mapping=can_mapping)

    def call(client, request, label):
        if isinstance(request, ManageLifecycleNodes.Request):
            events.append(('lifecycle', request.command))
            return SimpleNamespace(success=True)
        if isinstance(request, SetInitialPose.Request):
            events.append(('initial_pose', request.pose))
            return SetInitialPose.Response()
        events.append(('load_map', request.map_url))
        return SimpleNamespace(result=load_result)

    monkeypatch.setattr(controller, '_call', call)
    return controller, events, modes, node


def _map(tmp_path, name='home.yaml'):
    path = tmp_path / name
    path.write_text('image: home.pgm\n')
    return str(path)


def test_start_without_map_runs_only_slam(monkeypatch):
    """No saved map selected: SLAM owns map->odom and only mapping is allowed."""
    controller, events, modes, node = _controller(monkeypatch)
    controller.start('')
    assert events == ['slam_start']
    assert modes == [LocalizationMode.SWITCHING, LocalizationMode.SWITCHING,
                     LocalizationMode.MAPPING]
    assert json.loads(node.published[-1].data)['mode'] == 'MAPPING'


def test_default_map_uses_regular_localization_without_global_search(monkeypatch, tmp_path):
    """Unknown maps load normally and use AMCL, not SLAM or another TF publisher."""
    path = _map(tmp_path, 'default_map.yaml')
    controller, events, modes, node = _controller(monkeypatch, default_map=path)
    client = _Relocalize()
    controller._relocalize = client
    controller.start(path)
    assert events[:2] == ['slam_stop', ('lifecycle', ManageLifecycleNodes.Request.STARTUP)]
    assert events[2] == ('load_map', path)
    name, pose = events[3]
    assert name == 'initial_pose' and pose.header.frame_id == 'map'
    assert pose.pose.pose.position.x == pose.pose.pose.position.y == 0.0
    assert pose.pose.pose.orientation.w == 1.0
    assert client.goals == []
    assert modes[-1] is LocalizationMode.LOCALIZATION
    state = json.loads(node.published[-1].data)
    assert state['map'] == path and state['pose_ready']


def test_stop_during_default_initial_pose_does_not_restore_readiness(monkeypatch, tmp_path):
    """A late AMCL service result cannot undo the concurrent movement stop."""
    path = _map(tmp_path, 'default_map.yaml')
    controller, _, _, node = _controller(monkeypatch, default_map=path)
    original_call = controller._call

    def call(client, request, label):
        if isinstance(request, SetInitialPose.Request):
            controller.stop_movement()
        return original_call(client, request, label)

    monkeypatch.setattr(controller, '_call', call)
    controller.start(path)
    state = json.loads(node.published[-1].data)
    assert state['mode'] == 'LOCALIZATION' and state['map'] == path
    assert not state['pose_ready'] and not controller.stop_pending


def test_real_map_keeps_existing_pose_search_when_default_map_is_configured(
        monkeypatch, tmp_path):
    """The default-map fallback must not replace real-map relocalization."""
    controller, events, _, _ = _controller(
        monkeypatch, default_map=_map(tmp_path, 'default_map.yaml'))
    client = _Relocalize()
    controller._relocalize = client
    controller.start(_map(tmp_path, 'home.yaml'))
    assert len(client.goals) == 1
    assert not any(event[0] == 'initial_pose' for event in events)


def test_mapping_backend_returns_to_default_map_without_changing_navigation(monkeypatch, tmp_path):
    """Start SLAM on request, then stop it before reactivating regular AMCL."""
    path = _map(tmp_path, 'default_map.yaml')
    controller, events, _, _ = _controller(monkeypatch, default_map=path)
    controller.start(path)
    assert controller._start_mapping_request(None, SimpleNamespace()).success
    assert events[-2:] == [('lifecycle', ManageLifecycleNodes.Request.RESET), 'slam_start']
    assert controller.mode is LocalizationMode.MAPPING
    assert controller._stop_mapping_request(None, SimpleNamespace()).success
    assert controller.mode is LocalizationMode.LOCALIZATION and controller.map_path == path
    assert events[-4:-1] == [
        'slam_stop', ('lifecycle', ManageLifecycleNodes.Request.STARTUP), ('load_map', path)]
    assert events[-1][0] == 'initial_pose'


def test_owned_mapping_switch_does_not_allow_unrelated_map_selection(monkeypatch, tmp_path):
    """Permit only backend start/stop requests under AutoSLAM's BASE ownership."""
    path = _map(tmp_path, 'default_map.yaml')
    controller, _, _, _ = _controller(
        monkeypatch, busy=True, default_map=path, can_mapping=lambda: True)
    controller.start(path)
    assert controller._start_mapping_request(None, SimpleNamespace()).success
    request = LoadMap.Request(map_url=_map(tmp_path, 'home.yaml'))
    assert controller._load_map_request(request, LoadMap.Response()).result != 0
    assert controller._stop_mapping_request(None, SimpleNamespace()).success


@pytest.mark.parametrize('capabilities,allowed', [
    (['autoslam'], True), (['follow_person'], False), (['autoslam', 'follow_person'], False),
])
def test_mapping_guard_ignores_queued_replacement_only_for_own_cleanup(capabilities, allowed):
    """Queued missions cannot prevent the current AutoSLAM from releasing SLAM."""
    node = object.__new__(SystemManagerNode)
    node._lock = RLock()
    node._accepting_goals = True
    node._state = SimpleNamespace(movement_stopping=False, active=lambda: [SimpleNamespace(
        resources={ExecutionResource.BASE},
        capability=SimpleNamespace(capability_id=name)) for name in capabilities])
    node._scheduler = SimpleNamespace(base_busy=lambda: True)
    assert node._mapping_can_switch() is allowed
    assert not node._base_is_free()


@pytest.mark.parametrize('accepting,stopping', [(False, False), (True, True)])
def test_unowned_mapping_respects_global_stop_admission(accepting, stopping):
    """Only owned AutoSLAM cleanup may bypass the regular admission fence."""
    node = object.__new__(SystemManagerNode)
    node._lock = RLock()
    node._accepting_goals = accepting
    node._state = SimpleNamespace(movement_stopping=stopping, active=lambda: [])
    node._scheduler = SimpleNamespace(base_busy=lambda: False)
    node._admissions = {}
    node._localization = None
    assert not node._mapping_can_switch()


def test_selecting_a_map_stops_slam_before_amcl_and_back(monkeypatch, tmp_path):
    """Never run SLAM and AMCL together; RESET clears the saved map on return."""
    controller, events, modes, node = _controller(monkeypatch)
    controller.start('')
    request = LoadMap.Request()
    request.map_url = _map(tmp_path)
    response = controller._load_map_request(request, LoadMap.Response())
    assert response.result == LoadMap.Response.RESULT_SUCCESS
    assert events[1:] == ['slam_stop', ('lifecycle', ManageLifecycleNodes.Request.STARTUP),
                          ('load_map', request.map_url)]
    state = json.loads(node.published[-1].data)
    assert {key: state[key] for key in ('mode', 'map', 'message')} == {
        'mode': 'LOCALIZATION', 'map': request.map_url,
        'message': 'saved map loaded; confirm the robot pose before driving'}
    response = controller._start_mapping_request(None, SimpleNamespace())
    assert response.success
    assert events[-2:] == [('lifecycle', ManageLifecycleNodes.Request.RESET), 'slam_start']
    assert modes[-1] is LocalizationMode.MAPPING


def test_switch_is_refused_while_the_base_is_in_use(monkeypatch, tmp_path):
    """Changing map->odom under a moving mission is refused, not queued."""
    controller, events, modes, _ = _controller(monkeypatch, busy=True)
    controller.start('')
    request = LoadMap.Request()
    request.map_url = _map(tmp_path)
    response = controller._load_map_request(request, LoadMap.Response())
    assert response.result == LoadMap.Response.RESULT_UNDEFINED_FAILURE
    assert events == ['slam_start'] and modes[-1] is LocalizationMode.MAPPING


@pytest.mark.parametrize('name', ['missing.yaml', 'home.pgm'])
def test_unknown_map_files_are_rejected_before_switching(monkeypatch, tmp_path, name):
    """A typo leaves the current localization running."""
    controller, events, _, _ = _controller(monkeypatch)
    controller.start('')
    (tmp_path / 'home.pgm').write_text('P2')
    request = LoadMap.Request()
    request.map_url = str(tmp_path / name)
    response = controller._load_map_request(request, LoadMap.Response())
    assert response.result == LoadMap.Response.RESULT_MAP_DOES_NOT_EXIST
    assert events == ['slam_start']


def test_rejected_map_reports_error_instead_of_localization(monkeypatch, tmp_path):
    """Map-requiring missions stay blocked when map_server cannot load it."""
    controller, _, modes, node = _controller(
        monkeypatch, load_result=LoadMap.Response.RESULT_INVALID_MAP_DATA)
    controller.start(_map(tmp_path))
    assert modes[-1] is LocalizationMode.ERROR
    assert json.loads(node.published[-1].data)['mode'] == 'ERROR'


class _Done:
    def __init__(self, value):
        self.value = value

    def done(self):
        return True

    def result(self):
        return self.value

    def add_done_callback(self, callback):
        callback(self)


class _Relocalize:
    """Stand-in /relocalize client that answers immediately."""

    def __init__(self, *, ready=True, accepted=True, success=True,
                 message='saved pose confirmed; 93% of the scan matches the map'):
        self.ready, self.accepted = ready, accepted
        self.result = SimpleNamespace(success=success, message=message)
        self.goals = []
        self.canceled = []

    def wait_for_server(self, timeout_sec):
        return self.ready

    def send_goal_async(self, goal):
        self.goals.append(goal)
        return _Done(SimpleNamespace(
            accepted=self.accepted, cancel_goal_async=lambda: self.canceled.append(goal),
            get_result_async=lambda: _Done(SimpleNamespace(
                result=self.result, status=GoalStatus.STATUS_SUCCEEDED))))


def test_changing_saved_maps_restarts_amcl_without_the_old_pose(monkeypatch, tmp_path):
    """AMCL particles from one map must not become the pose on another."""
    controller, events, _, _ = _controller(monkeypatch)
    controller.start(_map(tmp_path, 'first.yaml'))
    request = LoadMap.Request()
    request.map_url = _map(tmp_path, 'second.yaml')
    controller._load_map_request(request, LoadMap.Response())
    lifecycle = [event[1] for event in events if event[0] == 'lifecycle']
    assert lifecycle == [ManageLifecycleNodes.Request.STARTUP,
                         ManageLifecycleNodes.Request.RESET,
                         ManageLifecycleNodes.Request.STARTUP]
    assert events[-1] == ('load_map', request.map_url)


@pytest.mark.parametrize('client, message', [
    (_Relocalize(), 'saved map loaded; saved pose confirmed; 93% of the scan matches'),
    (_Relocalize(success=False, message='global search matched only 20% of the scan'),
     'pose not found (global search matched only 20% of the scan), set the initial pose'),
    (_Relocalize(ready=False), 'relocalization is unavailable'),
    (_Relocalize(accepted=False), 'another pose correction is running'),
])
def test_each_map_load_finds_the_pose_before_localization(
        monkeypatch, tmp_path, client, message):
    """The pose is found while SWITCHING; the outcome is the LOCALIZATION message."""
    controller, _, modes, node = _controller(monkeypatch)
    controller._relocalize = client
    controller.start(_map(tmp_path))
    states = [json.loads(item.data) for item in node.published]
    assert states[-2]['mode'] == 'SWITCHING'
    assert states[-2]['message'] == 'saved map loaded; finding the robot pose'
    assert states[-1]['mode'] == 'LOCALIZATION' and message in states[-1]['message']
    assert modes[-2:] == [LocalizationMode.SWITCHING, LocalizationMode.LOCALIZATION]
    assert [goal.method for goal in client.goals] == ([0] if client.ready else [])
    # Mapping never asks for a pose.
    controller._start_mapping_request(None, SimpleNamespace())
    assert len(client.goals) == (1 if client.ready else 0)


def test_without_relocalization_the_operator_sets_the_pose(monkeypatch, tmp_path):
    """Standalone or restore_pose:=false Bringup leaves the pose to RViz."""
    controller, _, _, node = _controller(monkeypatch)
    controller.start(_map(tmp_path))
    assert json.loads(node.published[-1].data)['message'] == (
        'saved map loaded; confirm the robot pose before driving')


def test_unfinished_pose_search_is_canceled_and_reported(monkeypatch, tmp_path):
    """A stuck relocalization cannot hold the switch forever."""
    controller, _, modes, node = _controller(monkeypatch)
    client = _Relocalize()
    pending = SimpleNamespace(done=lambda: False, add_done_callback=lambda _: None)
    handle = SimpleNamespace(accepted=True, get_result_async=lambda: pending,
                             cancel_goal_async=lambda: client.canceled.append('cancel'))
    client.send_goal_async = lambda goal: _Done(handle)
    controller._relocalize = client
    controller._relocalize_timeout_s = 0.05
    controller.start(_map(tmp_path))
    assert client.canceled == ['cancel']
    state = json.loads(node.published[-1].data)
    assert state['mode'] == 'LOCALIZATION' and 'finding the pose failed' in state['message']
    assert not state['pose_ready'] and controller.stop_pending


def test_localization_status_has_identity_and_only_verified_pose_is_ready(monkeypatch, tmp_path):
    """A selected map and a succeeded pose check have separate readiness meaning."""
    controller, _, _, node = _controller(monkeypatch)
    controller._relocalize = _Relocalize()
    path = _map(tmp_path)
    controller.start(path)
    first = json.loads(node.published[-1].data)
    assert first['runtime_id'] and first['transition_id'] == 1 and first['pose_ready']
    controller._start_mapping_request(None, SimpleNamespace())
    second = json.loads(node.published[-1].data)
    assert second['runtime_id'] == first['runtime_id']
    assert second['transition_id'] == 2 and not second['pose_ready']


class _Pending:
    """A controllable future for the internal Action acceptance race."""

    def __init__(self):
        self.value = None
        self.callbacks = []

    def done(self):
        return self.value is not None

    def result(self):
        return self.value

    def add_done_callback(self, callback):
        self.callbacks.append(callback)
        if self.done():
            callback(self)

    def resolve(self, value):
        self.value = value
        for callback in self.callbacks:
            callback(self)


def test_stop_cancels_late_internal_pose_goal_and_waits_for_terminal(monkeypatch, tmp_path):
    """Stopping during map selection must cover its late accepted rotation."""
    controller, _, _, _ = _controller(monkeypatch)
    accepted, terminal = _Pending(), _Pending()
    cancels = []
    controller._relocalize = SimpleNamespace(
        wait_for_server=lambda **_: True, send_goal_async=lambda _: accepted)
    thread = Thread(target=controller.start, args=(_map(tmp_path),))
    thread.start()
    deadline = time.monotonic() + 2.0
    while controller._relocalize_goal_future is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert controller._relocalize_goal_future is accepted
    identity = controller.movement_identity
    controller.stop_movement()
    assert controller.stop_pending and controller.movement_pending(identity)
    accepted.resolve(SimpleNamespace(
        accepted=True, cancel_goal_async=lambda: cancels.append(True),
        get_result_async=lambda: terminal))
    assert cancels == [True]
    assert controller.stop_pending
    terminal.resolve(SimpleNamespace(
        status=GoalStatus.STATUS_CANCELED,
        result=SimpleNamespace(success=False, message='canceled')))
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert not controller.stop_pending and not controller.pose_ready


def test_stop_before_startup_fences_initial_pose_action(monkeypatch, tmp_path):
    """The initial timer cannot create a rotation after a preceding stop."""
    controller, _, _, _ = _controller(monkeypatch)
    sent = []
    controller._relocalize = SimpleNamespace(
        wait_for_server=lambda **_: True, send_goal_async=sent.append)
    identity = controller.movement_identity
    controller.stop_movement()
    assert identity and controller.stop_pending
    controller.start(_map(tmp_path))
    assert sent == [] and not controller.pose_ready
    assert not controller.movement_pending(identity)
    assert not controller.stop_pending


def test_map_admission_and_switching_state_share_manager_lock(monkeypatch, tmp_path):
    """BASE checks and reserving a switch form one admission transaction."""
    controller, _, _, _ = _controller(monkeypatch)
    controller.start('')
    lock = RLock()
    controller._admission_lock = lock
    checks = []

    def can_switch():
        checks.append(lock._is_owned())
        return True

    controller._can_switch = can_switch
    controller._on_mode = lambda mode: checks.append(
        lock._is_owned() if mode is LocalizationMode.SWITCHING else True)
    success, _ = controller._switch(_map(tmp_path))
    assert success and checks[0:2] == [True, True]


@pytest.mark.parametrize('mapping', [True, False])
def test_integrated_preparation_rejects_stale_epoch_before_any_transition(
    monkeypatch, tmp_path, mapping,
):
    """A queued map or mapping request cannot reserve new work after a stop."""
    controller, events, _, _ = _controller(monkeypatch)
    controller.start('')
    controller._movement_state = lambda: ('manager', 3)
    request = PrepareLocalization.Request(
        mapping=mapping, map_url='' if mapping else _map(tmp_path),
        movement_runtime_id='manager', movement_epoch=2)
    response = controller._prepare_request(request, PrepareLocalization.Response())
    assert not response.success and response.code == 'movement_epoch_changed'
    assert events == ['slam_start']
    assert controller.transition_id == 1 and controller.mode is LocalizationMode.MAPPING


def test_integrated_binding_check_and_transition_reservation_are_atomic(monkeypatch, tmp_path):
    """The epoch cannot advance between authorization and reserving the switch."""
    controller, _, _, _ = _controller(monkeypatch)
    controller.start('')
    lock, checks = RLock(), []
    controller._admission_lock = lock

    def movement_state():
        checks.append(lock._is_owned())
        return 'manager', 3

    def can_switch():
        checks.append(lock._is_owned())
        return True

    controller._movement_state = movement_state
    controller._can_switch = can_switch
    controller._on_mode = lambda _: checks.append(lock._is_owned())
    request = PrepareLocalization.Request(
        map_url=_map(tmp_path), movement_runtime_id='manager', movement_epoch=3)
    response = controller._prepare_request(request, PrepareLocalization.Response())
    assert response.success and response.code == 'completed'
    assert checks[:3] == [True, True, True]
