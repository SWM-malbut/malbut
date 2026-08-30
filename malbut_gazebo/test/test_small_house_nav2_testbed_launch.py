"""Contracts for the default-off Small House Nav2 testbed."""

import importlib.util
import json
from pathlib import Path
import shutil

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

from malbut_gazebo.map_lifecycle import MAP_STORE_FORMAT
from malbut_gazebo.user_map_builder import load_slam_map


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


def _context_with_defaults(description, **overrides):
    context = LaunchContext()
    context.launch_configurations.update(_defaults(description))
    context.launch_configurations.update(overrides)
    return context


def _runtime_map_store(tmp_path):
    """Create one writable, exact Small House map-store fixture."""
    map_store = tmp_path / 'map-store'
    map_store.mkdir(mode=0o700)
    source_map = PACKAGE_ROOT / 'maps' / 'small_house.yaml'
    source_image = PACKAGE_ROOT / 'maps' / 'small_house.pgm'
    map_yaml = map_store / source_map.name
    map_image = map_store / source_image.name
    shutil.copy2(source_map, map_yaml)
    shutil.copy2(source_image, map_image)
    slam_map = load_slam_map(map_yaml)
    user_map = map_store / 'user-map.geojson'
    user_map.write_text(json.dumps({
        'type': 'FeatureCollection',
        'format': 'malbut-user-map-v1',
        'map_id': slam_map.map_id,
        'map_revision': slam_map.map_revision,
        'frame_id': 'map',
        'features': [],
    }), encoding='utf-8')
    (map_store / 'active.json').write_text(json.dumps({
        'format': MAP_STORE_FORMAT,
        'map_id': slam_map.map_id,
        'map_revision': slam_map.map_revision,
        'map_yaml': map_yaml.name,
        'map_image': map_image.name,
        'user_map': user_map.name,
    }), encoding='utf-8')
    return map_store, user_map, slam_map


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
    assert defaults['enable_named_navigation'] == 'false'
    assert defaults['named_navigation_user_map'] == ''
    assert defaults['named_navigation_map_store'] == ''
    assert defaults['named_navigation_port'] == '8765'
    assert defaults['named_navigation_test_unavailable_action'] == 'false'

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
    assert _resolve(context, world['lidar_enabled']) == 'true'
    assert _resolve(context, world['depth_camera_enabled']) == 'false'
    assert Path(_resolve(context, navigation['map'])).name == (
        'small_house.yaml'
    )
    assert _resolve(context, navigation['depth_costmap_enabled']) == 'false'
    assert _resolve(context, navigation['localization_source']) == 'static'
    assert _resolve(context, navigation['namespace']) == ''
    assert _resolve(context, navigation['use_namespace']) == 'false'
    assert _resolve(context, navigation['autostart']) == 'false'
    assert _resolve(context, navigation['restore_localization']) == 'false'
    assert _resolve(context, navigation['set_initial_pose']) == 'true'
    assert _resolve(
        context,
        navigation['robot_web_navigation_action'],
    ) == '/navigate_to_pose'

    for world_name, navigation_name in (
        ('x', 'initial_pose_x'),
        ('y', 'initial_pose_y'),
        ('yaw', 'initial_pose_yaw'),
    ):
        assert _resolve(context, world[world_name]) == _resolve(
            context,
            navigation[navigation_name],
        )


def test_unavailable_navigation_action_is_fixed_default_off_and_opt_in():
    description = _load_launch()
    navigation = dict(next(
        entity
        for entity in _all_entities(description)
        if isinstance(entity, IncludeLaunchDescription)
        and _source_name(entity) == 'navigation.launch.py'
    ).launch_arguments)

    normal = _context_with_defaults(description)
    injected = _context_with_defaults(description, **{
        'enable_named_navigation': 'true',
        'named_navigation_test_unavailable_action': 'true',
    })

    assert _resolve(
        normal,
        navigation['robot_web_navigation_action'],
    ) == '/navigate_to_pose'
    assert _resolve(
        injected,
        navigation['robot_web_navigation_action'],
    ) == '/swm25_138_unavailable_navigate_to_pose'


def test_isolation_guard_precedes_every_process_creating_action(monkeypatch):
    module = _load_launch_module()
    description = module.generate_launch_description()
    entities = list(description.entities)
    guard_indexes = [
        index
        for index, entity in enumerate(entities)
        if isinstance(entity, OpaqueFunction)
    ]
    process_indexes = [
        index
        for index, entity in enumerate(entities)
        if isinstance(entity, (GroupAction, RegisterEventHandler, Node))
    ]

    assert len(guard_indexes) == 2
    assert process_indexes
    assert all(
        guard_index < process_index
        for guard_index in guard_indexes
        for process_index in process_indexes
    )

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

    context = _context_with_defaults(description, **{
        'autonomous_modes': 'true',
        'robot_web': 'true',
        'robot_web_port': '9999',
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
    assert _resolve(context, navigation['boot_pose_trusted']) == 'false'
    assert _resolve(context, navigation['robot_web_port']) == '8765'
    assert _resolve(
        context,
        navigation['robot_web_navigation_action'],
    ) == '/navigate_to_pose'

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


def test_explicit_named_navigation_only_enables_web_and_pose_validation():
    """The opt-in must not revive any autonomous motion owner."""
    description = _load_launch()
    navigation_include = next(
        entity
        for entity in _all_entities(description)
        if isinstance(entity, IncludeLaunchDescription)
        and _source_name(entity) == 'navigation.launch.py'
    )
    navigation = dict(navigation_include.launch_arguments)
    context = _context_with_defaults(description, **{
        'enable_named_navigation': 'true',
        'named_navigation_user_map': '/runtime/user-map.geojson',
        'named_navigation_map_store': '/runtime/map-store',
        'named_navigation_port': '8876',
        'autonomous_modes': 'true',
        'person_following': 'true',
        'inscribed_escape_enabled': 'true',
        'patrol_route_file': '/unsafe/patrol.yaml',
    })
    slam_map = load_slam_map(PACKAGE_ROOT / 'maps' / 'small_house.yaml')

    assert _resolve(context, navigation['robot_web']) == 'true'
    assert _resolve(context, navigation['robot_web_port']) == '8876'
    assert _resolve(
        context,
        navigation['robot_web_navigation_action'],
    ) == '/navigate_to_pose'
    assert _resolve(context, navigation['robot_web_device_id']) == (
        'malbut-sim-01'
    )
    assert _resolve(context, navigation['robot_web_simulation']) == 'true'
    assert _resolve(context, navigation['user_map']) == (
        '/runtime/user-map.geojson'
    )
    assert _resolve(context, navigation['pose_checkpoint_store']) == (
        '/runtime/map-store'
    )
    assert _resolve(context, navigation['pose_checkpoint_map_id']) == (
        slam_map.map_id
    )
    assert _resolve(
        context,
        navigation['pose_checkpoint_map_revision'],
    ) == slam_map.map_revision
    assert _resolve(context, navigation['boot_pose_trusted']) == 'true'
    assert _resolve(context, navigation['autonomous_modes']) == 'false'
    assert _resolve(context, navigation['patrol_route_file']) == ''
    assert _resolve(context, navigation['person_following']) == 'false'
    assert _resolve(
        context,
        navigation['inscribed_escape_enabled'],
    ) == 'false'


def test_named_navigation_preflight_accepts_only_exact_runtime_binding(
    tmp_path,
):
    """A writable exact map binding passes while the default stays inert."""
    module = _load_launch_module()
    description = module.generate_launch_description()
    assert module._validate_named_navigation_configuration(
        _context_with_defaults(description)
    ) == []

    map_store, user_map, _slam_map = _runtime_map_store(tmp_path)
    context = _context_with_defaults(description, **{
        'enable_named_navigation': 'true',
        'named_navigation_user_map': str(user_map),
        'named_navigation_map_store': str(map_store),
        'named_navigation_port': '8876',
    })
    assert module._validate_named_navigation_configuration(context) == []


def test_named_navigation_preflight_rejects_ambiguous_or_changed_input(
    tmp_path,
):
    """Disabled, malformed, and revision-drifted inputs fail closed."""
    module = _load_launch_module()
    description = module.generate_launch_description()
    map_store, user_map, slam_map = _runtime_map_store(tmp_path)

    disabled = _context_with_defaults(description, **{
        'named_navigation_user_map': str(user_map),
        'named_navigation_map_store': str(map_store),
    })
    with pytest.raises(RuntimeError) as caught:
        module._validate_named_navigation_configuration(disabled)
    assert str(caught.value) == 'named_navigation_disabled_configuration'

    disabled_fault = _context_with_defaults(description, **{
        'named_navigation_test_unavailable_action': 'true',
    })
    with pytest.raises(RuntimeError) as caught:
        module._validate_named_navigation_configuration(disabled_fault)
    assert str(caught.value) == 'named_navigation_disabled_configuration'

    malformed_fault = _context_with_defaults(description, **{
        'named_navigation_test_unavailable_action': 'sometimes',
    })
    with pytest.raises(RuntimeError) as caught:
        module._validate_named_navigation_configuration(malformed_fault)
    assert str(caught.value) == (
        'named_navigation_test_unavailable_action_invalid'
    )

    malformed_port = _context_with_defaults(description, **{
        'enable_named_navigation': 'true',
        'named_navigation_user_map': str(user_map),
        'named_navigation_map_store': str(map_store),
        'named_navigation_port': '0',
    })
    with pytest.raises(RuntimeError) as caught:
        module._validate_named_navigation_configuration(malformed_port)
    assert str(caught.value) == 'named_navigation_port_invalid'

    changed = json.loads(user_map.read_text(encoding='utf-8'))
    changed['map_revision'] = f'{slam_map.map_revision}-changed'
    user_map.write_text(json.dumps(changed), encoding='utf-8')
    mismatch = _context_with_defaults(description, **{
        'enable_named_navigation': 'true',
        'named_navigation_user_map': str(user_map),
        'named_navigation_map_store': str(map_store),
    })
    with pytest.raises(RuntimeError) as caught:
        module._validate_named_navigation_configuration(mismatch)
    assert str(caught.value) == 'named_navigation_user_map_mismatch'


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
