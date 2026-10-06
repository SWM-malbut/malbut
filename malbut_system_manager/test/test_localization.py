"""Unit tests for localization switching without Nav2 or slam_toolbox."""

import json
from threading import RLock
from types import SimpleNamespace

from builtin_interfaces.msg import Time
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
    assert modes == [LocalizationMode.SWITCHING, LocalizationMode.MAPPING]
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
    assert json.loads(node.published[-1].data)['map'] == path


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
    node._state = SimpleNamespace(active=lambda: [SimpleNamespace(
        resources={ExecutionResource.BASE},
        capability=SimpleNamespace(capability_id=name)) for name in capabilities])
    node._scheduler = SimpleNamespace(base_busy=lambda: True)
    assert node._mapping_can_switch() is allowed
    assert not node._base_is_free()


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
    assert json.loads(node.published[-1].data) == {
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
            get_result_async=lambda: _Done(SimpleNamespace(result=self.result))))


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
    pending = SimpleNamespace(done=lambda: False)
    handle = SimpleNamespace(accepted=True, get_result_async=lambda: pending,
                             cancel_goal_async=lambda: client.canceled.append('cancel'))
    client.send_goal_async = lambda goal: _Done(handle)
    controller._relocalize = client
    controller._relocalize_timeout_s = 0.05
    controller.start(_map(tmp_path))
    assert client.canceled == ['cancel']
    state = json.loads(node.published[-1].data)
    assert state['mode'] == 'LOCALIZATION' and 'finding the pose failed' in state['message']
