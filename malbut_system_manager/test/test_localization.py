"""Unit tests for localization switching without Nav2 or slam_toolbox."""

import json
from types import SimpleNamespace

from nav2_msgs.srv import LoadMap, ManageLifecycleNodes
import pytest

from malbut_system_manager.localization import LocalizationController
from malbut_system_manager.models import LocalizationMode


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


def _controller(monkeypatch, *, busy=False, load_result=LoadMap.Response.RESULT_SUCCESS):
    events, modes = [], []
    node = _Node()
    controller = LocalizationController(
        node, None, slam=_Slam(events), on_mode=modes.append,
        can_switch=lambda: not busy, lifecycle_service='/manage',
        map_server_load_service='/load', service_timeout_s=1.0)

    def call(client, request, label):
        if isinstance(request, ManageLifecycleNodes.Request):
            events.append(('lifecycle', request.command))
            return SimpleNamespace(success=True)
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
