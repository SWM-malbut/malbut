"""Package, map, launch, and interface contracts for SWM25-94."""

import importlib.util
import json
import math
from pathlib import Path
from threading import Lock, RLock
from types import SimpleNamespace
from xml.etree import ElementTree

import cv2
from launch import LaunchContext
from launch.actions import (
    DeclareLaunchArgument,
    GroupAction,
    IncludeLaunchDescription,
    RegisterEventHandler,
)
from launch.utilities import perform_substitutions
from launch_ros.actions import Node, SetRemap
import yaml

from malbut_gazebo.user_map_builder import load_slam_map
from malbut_gazebo.zone_filter_mask import build_filter_mask, load_zones
from malbut_scenarios.scenario_config import load_room_routes
from malbut_scenarios.autonomous_driving_manager import (
    AutonomousDrivingManager,
    ScenarioMode,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPOSITORY_ROOT / 'malbut_scenarios'
GAZEBO_ROOT = REPOSITORY_ROOT / 'malbut_gazebo'


class ControlledFuture:
    def __init__(self):
        self._callbacks = []

    def add_done_callback(self, callback):
        self._callbacks.append(callback)

    @staticmethod
    def result():
        return None

    def resolve(self):
        for callback in self._callbacks:
            callback(self)


def _scenario_manager_stub(mode=ScenarioMode.PATROLLING):
    manager = AutonomousDrivingManager.__new__(AutonomousDrivingManager)
    manager._lock = RLock()
    manager._mode = mode
    manager._detail = 'test'
    manager._tracking_token = 7
    manager._tracking_shutdown_pending = 0
    manager._transition_token = 0
    manager._transition_target = None
    manager._transition_remaining = 0
    manager._transition_detail = ''
    manager._room_waypoints = [object()]
    manager._active_room = 'living_room'
    manager._manual_active = True
    manager._actor_visible = False
    manager._roaming_stop = object()
    manager._roaming_start = object()
    return manager


def test_total_stop_cancels_all_actions_and_enters_idle():
    manager = _scenario_manager_stub()
    events = []
    manager._call_trigger = lambda client: None

    def cancel_all():
        events.append(('cancel_all', None))
        return []

    manager._cancel_all_actions = cancel_all
    manager._velocity_publisher = SimpleNamespace(
        publish=lambda message: events.append(('velocity', message))
    )
    manager._publish_status = lambda: events.append(('status', manager._mode))
    response = SimpleNamespace(success=False, message='')

    result = manager._stop_callback(None, response)

    assert result is response
    assert response.success is True
    assert response.message == 'stopping all scenario behavior'
    assert manager._tracking_token == 8
    assert manager._room_waypoints == []
    assert manager._active_room is None
    assert manager._manual_active is False
    assert manager._mode == ScenarioMode.IDLE
    assert ('cancel_all', None) in events


def test_new_mode_waits_until_previous_behavior_is_quiescent():
    manager = _scenario_manager_stub(ScenarioMode.PERSON_TRACKING)
    cancellation = ControlledFuture()
    events = []

    def trigger(client):
        events.append(('trigger', client))
        return cancellation if client is manager._roaming_stop else None

    manager._call_trigger = trigger
    manager._cancel_all_actions = lambda: []
    manager._velocity_publisher = SimpleNamespace(
        publish=lambda _message: None
    )
    manager._publish_status = lambda: None
    response = SimpleNamespace(success=False, message='')

    manager._start_patrol_callback(None, response)

    assert response.success is True
    assert manager._mode == ScenarioMode.TRANSITIONING
    assert ('trigger', manager._roaming_start) not in events

    cancellation.resolve()

    assert manager._mode == ScenarioMode.PATROLLING
    assert ('trigger', manager._roaming_start) in events


def test_idle_mode_drops_late_nav_velocity_commands():
    published = []
    manager = AutonomousDrivingManager.__new__(AutonomousDrivingManager)
    manager._lock = RLock()
    manager._manual_active = False
    manager._mode = ScenarioMode.IDLE
    manager._velocity_publisher = SimpleNamespace(publish=published.append)

    manager._nav_velocity_callback(object())
    assert published == []

    manager._mode = ScenarioMode.PERSON_TRACKING
    command = object()
    manager._nav_velocity_callback(command)
    assert published == [command]


def test_person_toggle_uses_verified_world_state_for_both_directions():
    class FakeActor:
        visible = False
        operations = []

        def exists(self):
            return self.visible

        def spawn(self):
            self.operations.append('spawn')
            self.visible = True

        def remove(self):
            self.operations.append('remove')
            self.visible = False

    manager = AutonomousDrivingManager.__new__(AutonomousDrivingManager)
    manager._actor_lock = Lock()
    manager._actor = FakeActor()
    manager._actor_visible = False
    manager._publish_status = lambda: None
    response = SimpleNamespace(success=False, message='')

    manager._toggle_person_callback(None, response)
    assert response.success is True
    assert manager._actor_visible is True
    assert manager._actor.operations == ['spawn']

    manager._toggle_person_callback(None, response)
    assert response.success is True
    assert manager._actor_visible is False
    assert manager._actor.operations == ['spawn', 'remove']


def _load_launch():
    launch_path = PACKAGE_ROOT / 'launch' / 'autonomous_driving.launch.py'
    spec = importlib.util.spec_from_file_location(
        'malbut_autonomous_driving_launch', launch_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.generate_launch_description()


def _source_name(include):
    source = include.launch_description_source
    source.get_launch_description(LaunchContext())
    return Path(source.location).name


def test_package_declares_only_public_ros_dependencies():
    root = ElementTree.parse(PACKAGE_ROOT / 'package.xml').getroot()
    dependencies = {
        element.text
        for element in root
        if element.tag in {'depend', 'exec_depend'}
    }

    assert {
        'malbut_gazebo',
        'malbut_gazebo_plugins',
        'malbut_interfaces',
        'malbut_perception',
        'malbut_roaming',
        'malbut_tracking',
        'nav2_msgs',
        'rclpy',
        'rviz2',
    } <= dependencies
    assert 'ros_gz_interfaces' not in dependencies


def test_one_launch_composes_existing_features_and_velocity_arbitration():
    description = _load_launch()
    context = LaunchContext()
    defaults = {
        entity.name: perform_substitutions(context, entity.default_value)
        for entity in description.entities
        if isinstance(entity, DeclareLaunchArgument)
    }
    assert 'actor_spawn_delay' not in defaults
    assert defaults['cloud_sync'] == 'false'
    assert defaults['patrol_autostart'] == 'false'
    assert defaults['rviz'] == 'true'
    assert defaults['zone_mask'] == ''
    context.launch_configurations.update(defaults)

    top_level_groups = [
        entity
        for entity in description.entities
        if isinstance(entity, GroupAction)
    ]
    assert len(top_level_groups) == 1
    simulation_includes = [
        entity
        for entity in top_level_groups[0].get_sub_entities()
        if isinstance(entity, IncludeLaunchDescription)
    ]
    include_names = {_source_name(item) for item in simulation_includes}
    assert include_names == {'worlds.launch.py'}

    readiness = next(
        entity
        for entity in description.entities
        if isinstance(entity, Node)
        and entity.node_executable == 'wait_for_simulation'
    )
    readiness_handler = next(
        entity.event_handler
        for entity in description.entities
        if isinstance(entity, RegisterEventHandler)
        and entity.event_handler.__dict__.get(
            '_OnActionEventBase__action_matcher'
        ) is readiness
    )
    runtime = readiness_handler.__dict__[
        '_OnActionEventBase__on_event'
    ](SimpleNamespace(returncode=0), context)
    runtime_includes = [
        entity
        for entity in runtime
        if isinstance(entity, IncludeLaunchDescription)
    ]
    runtime_include_names = {
        _source_name(item) for item in runtime_includes
    }
    assert runtime_include_names == {
        'person_detection.launch.py',
        'roaming.launch.py',
        'person_following.launch.py',
    }

    navigation_group = next(
        entity
        for entity in runtime
        if isinstance(entity, GroupAction)
    )
    grouped = navigation_group.get_sub_entities()
    remap = next(
        action
        for action in grouped
        if isinstance(action, SetRemap)
    )
    assert perform_substitutions(
        context, remap._SetRemap__src
    ) == '/cmd_vel'
    assert perform_substitutions(
        context, remap._SetRemap__dst
    ) == '/scenario/nav_cmd_vel'
    navigation = next(
        action
        for action in grouped
        if isinstance(action, IncludeLaunchDescription)
    )
    arguments = dict(navigation.launch_arguments)
    assert arguments['use_composition'] == 'True'
    assert arguments['robot_web_navigation_action'] == (
        '/scenario/navigate_to_pose'
    )
    assert perform_substitutions(
        context, [arguments['zone_mask']]
    ) == ''
    assert str(arguments['user_map']).endswith(
        '/maps/small_house_user_map.geojson'
    )

    nodes = [
        entity for entity in runtime if isinstance(entity, Node)
    ]
    executables = {entity.node_executable for entity in nodes}
    assert {
        'autonomous_driving_manager',
        'cloud_robot_sync',
        'collision_monitor',
        'lifecycle_manager',
        'rviz2',
        'rqt_image_view',
    } <= executables

    manager = next(
        entity
        for entity in nodes
        if entity.node_executable == 'autonomous_driving_manager'
    )
    normalized_parameters = manager._Node__parameters[1]
    actor_parameters = {
        perform_substitutions(context, name): (
            yaml.safe_load(perform_substitutions(context, value))
            if isinstance(value, tuple)
            else value
        )
        for name, value in normalized_parameters.items()
    }
    assert actor_parameters['actor_entity_name'] == 'scenario_humanoid'
    assert actor_parameters['actor_service_prefix'] == (
        '/world/small_house/scenario_actor'
    )
    assert str(actor_parameters['actor_file']).endswith(
        '/humanoid_actor/scenarios/front_door_entry.sdf'
    )
    assert actor_parameters['actor_x'] == 6.0
    assert actor_parameters['actor_y'] == -6.2


def test_small_house_loads_verified_actor_control_system():
    world = ElementTree.parse(
        GAZEBO_ROOT / 'worlds' / 'small_house.sdf'
    ).getroot().find('world')
    plugin = next(
        item
        for item in world.findall('plugin')
        if item.get('filename') == 'libactor_control_system.so'
    )

    assert plugin.get('name') == 'malbut::gazebo::ActorControlSystem'
    assert plugin.findtext('actor_name') == 'scenario_humanoid'
    assert plugin.findtext('service_prefix') == (
        '/world/small_house/scenario_actor'
    )


def test_scenario_humanoid_route_is_clear_and_loops_without_a_jump():
    actor_file = (
        GAZEBO_ROOT
        / 'models'
        / 'humanoid_actor'
        / 'scenarios'
        / 'front_door_entry.sdf'
    )
    actor = ElementTree.parse(actor_file).getroot().find('actor')
    assert actor.findtext('script/loop') == 'true'
    animations = {
        item.get('name'): item for item in actor.findall('animation')
    }
    trajectories = actor.findall('script/trajectory')
    assert trajectories
    assert {'walking', 'stand'} <= animations.keys()
    assert animations['walking'].findtext('interpolate_x') == 'true'
    assert animations['stand'].findtext('interpolate_x') == 'true'
    assert [int(item.get('id')) for item in trajectories] == list(
        range(len(trajectories))
    )

    local_poses = []
    total_duration = 0.0
    diagonal_segments = 0
    for trajectory in trajectories:
        assert trajectory.get('tension') == '1.0'
        waypoints = trajectory.findall('waypoint')
        assert len(waypoints) == 2
        times = [
            float(waypoint.findtext('time')) for waypoint in waypoints
        ]
        assert times[0] == 0.0
        assert times[1] > 0.0
        poses = [
            [float(value) for value in waypoint.findtext('pose').split()]
            for waypoint in waypoints
        ]
        if local_poses:
            assert poses[0] == local_poses[-1]
        else:
            local_poses.append(poses[0])
        local_poses.append(poses[1])
        total_duration += times[1]

        start, end = poses
        distance = math.dist(start[:2], end[:2])
        if distance > 1e-6:
            assert trajectory.get('type') == 'walking'
            assert math.isclose(
                distance / times[1], 0.45, rel_tol=0.0, abs_tol=0.002
            )
            travel_heading = math.atan2(
                end[1] - start[1], end[0] - start[0]
            )
            if not math.isclose(
                math.sin(2.0 * travel_heading), 0.0, abs_tol=1e-5
            ):
                diagonal_segments += 1
            for actor_yaw in (start[5], end[5]):
                facing_error = math.atan2(
                    math.sin(actor_yaw - travel_heading),
                    math.cos(actor_yaw - travel_heading),
                )
                assert math.isclose(
                    facing_error, 0.0, rel_tol=0.0, abs_tol=1e-5
                )
            continue
        assert trajectory.get('type') == 'stand'
        yaw_delta = abs(
            math.atan2(
                math.sin(end[5] - start[5]),
                math.cos(end[5] - start[5]),
            )
        )
        assert times[1] >= 0.349
        assert yaw_delta / times[1] <= 1.202

    assert diagonal_segments > 0
    world_points = [
        (6.0 + pose[0], -6.2 + pose[1]) for pose in local_poses
    ]
    assert world_points[0] == (6.0, -6.2)
    assert world_points[-1] == world_points[0]
    assert local_poses[-1] == local_poses[0]
    assert 125.0 <= total_duration <= 128.0

    # Door_01 is the front door at map (6.0, -5.55). The first segment
    # deliberately crosses its threshold from outside to inside.
    assert math.isclose(world_points[1][0], 6.0, abs_tol=1e-9)
    assert math.isclose(world_points[1][1], -4.8, abs_tol=1e-9)
    assert {
        'kitchen_aisle': any(
            x > 6.5 and y < -2.0 for x, y in world_points
        ),
        'dining_aisle': any(
            x > 4.5 and y > 1.5 for x, y in world_points
        ),
        'gym_threshold': any(
            1.8 < x < 2.2 and 2.4 < y < 2.7
            for x, y in world_points
        ),
        'central_living': any(
            -2.5 < x < 1.1 and 0.2 < y < 1.6
            for x, y in world_points
        ),
        'bedroom_threshold': any(
            x < -7.0 and 0.7 < y < 1.0 for x, y in world_points
        ),
        'south_living': any(
            -2.5 < x < 0.6 and y < -1.0 for x, y in world_points
        ),
    } == {
        'kitchen_aisle': True,
        'dining_aisle': True,
        'gym_threshold': True,
        'central_living': True,
        'bedroom_threshold': True,
        'south_living': True,
    }
    assert not any(
        x < -3.0 and y > 1.0 for x, y in world_points
    ), 'scenario actor must not enter the narrow bedside area'
    assert not any(
        1.5 < x < 4.5 and y > 3.0 for x, y in world_points
    ), 'scenario actor must not enter the inner gym area'


def test_demo_user_map_and_web_zone_pipeline_share_one_map_identity():
    slam_map = load_slam_map(
        GAZEBO_ROOT / 'maps' / 'small_house.yaml'
    )
    user_map = json.loads((
        PACKAGE_ROOT / 'maps' / 'small_house_user_map.geojson'
    ).read_text(encoding='utf-8'))
    zone_path = (
        PACKAGE_ROOT
        / 'maps'
        / f'{slam_map.map_id}-zones.geojson'
    )
    zones = load_zones(
        zone_path,
        slam_map.map_id,
        slam_map.map_revision,
        slam_map.legacy_map_ids,
    )
    mask = yaml.safe_load((
        PACKAGE_ROOT / 'maps' / 'zone-filter.yaml'
    ).read_text(encoding='utf-8'))

    assert user_map['map_id'] == slam_map.map_id
    assert user_map['map_revision'] == slam_map.map_revision
    assert isinstance(zones, list)
    assert mask['image'] == 'zone-filter.pgm'
    assert mask['resolution'] == slam_map.transform.resolution
    assert mask['origin'] == [
        slam_map.transform.origin_x,
        slam_map.transform.origin_y,
        0.0,
    ]


def test_room_routes_are_free_and_outside_the_generated_keepout_mask():
    slam_map = load_slam_map(
        GAZEBO_ROOT / 'maps' / 'small_house.yaml'
    )
    zones = load_zones(
        PACKAGE_ROOT / 'maps' / f'{slam_map.map_id}-zones.geojson',
        slam_map.map_id,
        slam_map.map_revision,
        slam_map.legacy_map_ids,
    )
    mask = cv2.imread(
        str(PACKAGE_ROOT / 'maps' / 'zone-filter.pgm'),
        cv2.IMREAD_GRAYSCALE,
    )
    expected_mask = build_filter_mask(slam_map, zones)
    _, rooms = load_room_routes(
        PACKAGE_ROOT / 'config' / 'room_routes.yaml'
    )

    assert mask is not None
    assert mask.shape == expected_mask.shape
    assert (mask == expected_mask).all()

    for room in rooms:
        for waypoint in room.waypoints:
            pixel_x, pixel_y = slam_map.transform.pixel([
                waypoint.x, waypoint.y,
            ])
            assert slam_map.image[pixel_y, pixel_x] >= 250
            assert mask[pixel_y, pixel_x] == 0
