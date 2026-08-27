"""Contracts for the non-actuating Small House Nav2 testbed."""

import importlib.util
from pathlib import Path

import pytest
from launch import LaunchContext
from launch.actions import (
    DeclareLaunchArgument,
    EmitEvent,
    GroupAction,
    IncludeLaunchDescription,
    OpaqueFunction,
    RegisterEventHandler,
)
from launch.event_handlers import OnProcessExit
from launch.events import Shutdown
from launch.events.process import ProcessExited
from launch.utilities import (
    normalize_to_list_of_substitutions,
    perform_substitutions,
)
from launch_ros.actions import Node


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
LAUNCH_FILE = (
    PACKAGE_ROOT / 'launch' / 'small_house_nav2_testbed.launch.py'
)


def _load_launch_module():
    spec = importlib.util.spec_from_file_location(
        'malbut_small_house_nav2_testbed_launch',
        LAUNCH_FILE,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_launch():
    return _load_launch_module().generate_launch_description()


def _all_entities(description):
    entities = []
    for entity in description.entities:
        entities.append(entity)
        if isinstance(entity, GroupAction):
            entities.extend(entity.get_sub_entities())
    return entities


def _source_name(include):
    source = include.launch_description_source
    source.get_launch_description(LaunchContext())
    return Path(source.location).name


def _resolve(context, value):
    return perform_substitutions(
        context,
        normalize_to_list_of_substitutions(value),
    )


def _defaults(description):
    context = LaunchContext()
    return {
        entity.name: perform_substitutions(context, entity.default_value)
        for entity in description.entities
        if isinstance(entity, DeclareLaunchArgument)
    }


def _process_exit(gate, returncode):
    return ProcessExited(
        action=gate,
        returncode=returncode,
        name='nav2_startup_gate',
        cmd=['nav2_startup_gate'],
        cwd=str(PACKAGE_ROOT),
        env={},
        pid=4242,
    )


def _shutdown_events(actions):
    if actions is None:
        return []
    if not isinstance(actions, (list, tuple)):
        actions = [actions]
    return [
        action.event
        for action in actions
        if isinstance(action, EmitEvent)
        and isinstance(action.event, Shutdown)
    ]


def test_testbed_composes_only_small_house_and_static_navigation():
    description = _load_launch()
    entities = _all_entities(description)
    includes = [
        entity
        for entity in entities
        if isinstance(entity, IncludeLaunchDescription)
    ]
    by_source = {_source_name(include): include for include in includes}

    assert set(by_source) == {
        'worlds.launch.py',
        'navigation.launch.py',
    }

    defaults = _defaults(description)
    assert defaults['x'] == '-3.665503'
    assert defaults['y'] == '-0.4874'
    assert defaults['yaw'] == '0.0'

    context = LaunchContext()
    context.launch_configurations.update({
        **defaults,
        'x': '-1.25',
        'y': '2.5',
        'yaw': '0.75',
        'use_sim_time': 'true',
    })
    world = dict(by_source['worlds.launch.py'].launch_arguments)
    navigation = dict(
        by_source['navigation.launch.py'].launch_arguments
    )

    assert _resolve(context, world['world_name']) == 'small_house'
    assert _resolve(context, world['spawn_robot']) == 'true'
    assert _resolve(context, world['bridge']) == 'true'
    assert Path(_resolve(context, navigation['map'])).name == (
        'small_house.yaml'
    )
    assert _resolve(context, navigation['localization_source']) == 'static'
    assert _resolve(context, navigation['namespace']) == ''
    assert _resolve(context, navigation['use_namespace']) == 'false'
    assert _resolve(context, navigation['autostart']) == 'false'
    assert _resolve(context, navigation['restore_localization']) == 'false'
    assert _resolve(context, navigation['set_initial_pose']) == 'true'

    for world_name, navigation_name in (
        ('x', 'initial_pose_x'),
        ('y', 'initial_pose_y'),
        ('yaw', 'initial_pose_yaw'),
    ):
        assert _resolve(context, world[world_name]) == _resolve(
            context,
            navigation[navigation_name],
        )


def test_isolation_guard_precedes_every_process_creating_action(monkeypatch):
    module = _load_launch_module()
    description = module.generate_launch_description()
    entities = list(description.entities)
    guard_index = next(
        index
        for index, entity in enumerate(entities)
        if isinstance(entity, OpaqueFunction)
    )
    process_indexes = [
        index
        for index, entity in enumerate(entities)
        if isinstance(entity, (GroupAction, RegisterEventHandler, Node))
    ]

    assert process_indexes
    assert all(guard_index < index for index in process_indexes)

    monkeypatch.setenv('ROS_DOMAIN_ID', '0')
    monkeypatch.setenv('ROS_LOCALHOST_ONLY', '1')
    with pytest.raises(RuntimeError) as caught:
        module._validate_isolated_ros_context(LaunchContext())
    assert str(caught.value) == 'isolated_ros_domain_required'

    monkeypatch.setenv('ROS_DOMAIN_ID', '29')
    monkeypatch.setenv('ROS_LOCALHOST_ONLY', '1')
    assert module._validate_isolated_ros_context(LaunchContext()) == []


def test_testbed_defaults_off_and_hard_disables_automatic_motion_owners():
    description = _load_launch()
    includes = [
        entity
        for entity in _all_entities(description)
        if isinstance(entity, IncludeLaunchDescription)
    ]
    by_source = {_source_name(include): include for include in includes}
    navigation_include = by_source['navigation.launch.py']
    navigation = dict(navigation_include.launch_arguments)

    context = LaunchContext()
    context.launch_configurations.update({
        'autonomous_modes': 'true',
        'robot_web': 'true',
        'person_following': 'true',
        'rviz': 'true',
        'inscribed_escape_enabled': 'true',
        'zone_mask': '/unsafe/mask.yaml',
        'user_map': '/unsafe/map.geojson',
        'patrol_route_file': '/unsafe/patrol.yaml',
        'pose_checkpoint_store': '/unsafe/checkpoints',
    })

    for name in (
        'autonomous_modes',
        'robot_web',
        'person_following',
        'inscribed_escape_enabled',
    ):
        assert _resolve(context, navigation[name]) == 'false'
    for name in (
        'zone_mask',
        'user_map',
        'patrol_route_file',
        'pose_checkpoint_store',
        'pose_checkpoint_map_id',
        'pose_checkpoint_map_revision',
    ):
        assert _resolve(context, navigation[name]) == ''

    defaults = _defaults(description)
    assert defaults['rviz'] == 'false'
    default_context = LaunchContext()
    default_context.launch_configurations.update(defaults)
    assert _resolve(default_context, navigation['rviz']) == 'false'

    navigation_description = (
        navigation_include.launch_description_source
        .get_launch_description(LaunchContext())
    )
    declared = {
        entity.name
        for entity in navigation_description.entities
        if isinstance(entity, DeclareLaunchArgument)
    }
    assert 'inscribed_escape_enabled' in declared


def test_testbed_has_one_non_actuating_gate_and_no_forbidden_route():
    description = _load_launch()
    entities = _all_entities(description)
    gates = [entity for entity in entities if isinstance(entity, Node)]

    assert len(gates) == 1
    gate = gates[0]
    assert gate.node_package == 'malbut_gazebo'
    assert gate.node_executable == 'nav2_startup_gate'
    assert gate._Node__node_name == (
        'small_house_nav2_testbed_startup_gate'
    )

    source = LAUNCH_FILE.read_text(encoding='utf-8')
    for forbidden in (
        'malbut_roaming',
        'roaming_manager',
        '/roaming/',
        'malbut_agent_server',
        'NavigateToPose',
        'ActionClient',
        'send_goal_async',
    ):
        assert forbidden not in source


def test_gate_failure_shuts_down_but_success_does_not():
    description = _load_launch()
    entities = _all_entities(description)
    gate = next(entity for entity in entities if isinstance(entity, Node))
    registrations = [
        entity
        for entity in entities
        if isinstance(entity, RegisterEventHandler)
    ]

    assert len(registrations) == 1
    handler = registrations[0].event_handler
    assert isinstance(handler, OnProcessExit)

    success = _process_exit(gate, 0)
    assert handler.matches(success)
    assert _shutdown_events(handler.handle(success, LaunchContext())) == []

    failure = _process_exit(gate, 1)
    assert handler.matches(failure)
    shutdowns = _shutdown_events(
        handler.handle(failure, LaunchContext())
    )
    assert len(shutdowns) == 1
    assert shutdowns[0].reason
