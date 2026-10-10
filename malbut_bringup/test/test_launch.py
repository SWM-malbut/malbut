"""Check independent modules without hardware, inference, APIs, or motion."""

import importlib.util
import json
from pathlib import Path
import sys
from xml.etree import ElementTree

from launch import LaunchContext, LaunchDescription, LaunchService
from launch.actions import (
    DeclareLaunchArgument, ExecuteProcess, GroupAction, IncludeLaunchDescription,
    LogInfo, OpaqueFunction, SetEnvironmentVariable,
)
from launch.utilities import perform_substitutions
from launch_ros.actions import LoadComposableNodes, Node
from launch_ros.utilities import evaluate_parameters
import pytest

from malbut_bringup import launch_support

ROOT = Path(__file__).parents[2]


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name + '_launch', ROOT / 'malbut_bringup/launch' / (name + '.launch.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def launch_module(tmp_path, monkeypatch):
    """Supply fake vendor assets, but use the real Malbut launch files."""
    monkeypatch.delenv('HOMECAM_BACKEND_URL', raising=False)
    monkeypatch.delenv('HOMECAM_DEVICE_TOKEN_FILE', raising=False)
    monkeypatch.delenv('HOMECAM_DEVICE_ID', raising=False)
    monkeypatch.setenv('MALBUT_FALL_CONFIG', str(tmp_path / 'unconfigured-fall.json'))
    for name in ('slam/launch/include/robot.launch.py',):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('from launch import LaunchDescription\n'
                        'def generate_launch_description():\n'
                        '    return LaunchDescription()\n')
    source = ROOT / 'malbut_bringup/launch/robot.launch.py'
    spec = importlib.util.spec_from_file_location('robot_launch', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path / 'home')
    cache = tmp_path / 'home/.cache'
    monkeypatch.setenv('XDG_CACHE_HOME', str(cache))
    for name, environment in (('malbut_yolo', 'MALBUT_YOLO_RUNTIME'),
                              ('malbut_reid', 'MALBUT_REID_RUNTIME'),
                              ('malbut_fall_pose', 'MALBUT_FALL_POSE_RUNTIME')):
        runtime = cache / name / 'runtime'
        monkeypatch.setenv(environment, str(runtime))
        executable = runtime / 'bin/python'
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text('#!/bin/sh\nexit 0\n')
        executable.chmod(0o755)
    for name in ('yolo26n.pt', 'yolo26s-pose.onnx', 'osnet_ain_x1_0_msmt17.onnx'):
        model = cache / 'malbut_perception' / name
        model.parent.mkdir(parents=True, exist_ok=True)
        model.write_bytes(b'not executed during this launch composition test')

    def package_share(name):
        if name == 'homecam_media_agent':
            return str(ROOT / 'homecam_agent' / name)
        if name.startswith('malbut_'):
            return str(ROOT / name if (ROOT / name).is_dir()
                       else ROOT / 'malbut_autonomy' / name)
        return str(tmp_path / name)

    from malbut_bringup import launch_support, nav2_stack
    monkeypatch.setattr(launch_support, 'get_package_share_directory', package_share)
    monkeypatch.setattr(nav2_stack, 'get_package_share_directory', package_share)
    module.get_package_share_directory = package_share
    return module


def _context(module, **overrides):
    context = LaunchContext()
    context.launch_configurations.update(overrides)
    for action in module.generate_launch_description().entities:
        if isinstance(action, DeclareLaunchArgument):
            action.execute(context)
    return context


def _includes(actions):
    return [child for action in actions if isinstance(action, GroupAction)
            for child in action.get_sub_entities()
            if isinstance(child, IncludeLaunchDescription)]


def _nodes(actions, executable):
    return [item for item in actions if isinstance(item, Node)
            and item.node_executable == executable]


def _parameters(context, node):
    return evaluate_parameters(context, node._Node__parameters)[0]


def _core_actions(launch_module, context):
    return launch_module._setup(context)


def _included_modules(actions):
    return {Path(_source_path(item)).name.split('.')[0]:
            dict(item.launch_arguments) for item in _includes(actions)}


def _source_path(include):
    return perform_substitutions(
        LaunchContext(), include.launch_description_source._LaunchDescriptionSource__location)


def _module_setup(name, context):
    group = launch_support.module_actions(name, _load(name)._setup)
    callback = next(item for item in group.get_sub_entities() if isinstance(item, OpaqueFunction))
    return callback.execute(context)


def test_cloud_launch_starts_only_outbound_bridge():
    """Cloud connectivity does not start hardware, navigation, or a local HTTP port."""
    source = ROOT / 'malbut_bringup/launch/cloud.launch.py'
    spec = importlib.util.spec_from_file_location('cloud_launch', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    context = _context(module, backend_url='https://robot.example.com',
                       token_file='/protected/device.token', map_directory='/maps')
    actions = module.generate_launch_description().entities
    nodes = [action for action in actions if isinstance(action, Node)]
    assert len(nodes) == 1 and nodes[0].node_executable == 'robot_cloud_sync'
    assert not _includes(actions)
    parameters = evaluate_parameters(context, nodes[0]._Node__parameters)[0]
    assert parameters['use_sim_time'] is False
    assert parameters['map_topic'] == '/map'
    assert parameters['token_file'] == '/protected/device.token'
    assert 'token' not in parameters and 'port' not in parameters
    for action in actions:
        if isinstance(action, SetEnvironmentVariable):
            action.execute(context)
    assert context.environment['HOMECAM_BACKEND_URL'] == 'https://robot.example.com'
    assert context.environment['HOMECAM_DEVICE_TOKEN_FILE'] == '/protected/device.token'


def test_camera_dds_profile_is_scoped_to_vendor_hardware(launch_module):
    """Only newly launched vendor hardware inherits the larger SHM segment."""
    context = _context(launch_module)
    context.environment['FASTRTPS_DEFAULT_PROFILES_FILE'] = '/existing/profile.xml'
    context.environment['RMW_IMPLEMENTATION'] = 'rmw_fastrtps_cpp'
    context.environment.pop('RMW_FASTRTPS_USE_QOS_FROM_XML', None)
    actions = _core_actions(launch_module, context)
    hardware = next(action for action in actions if isinstance(action, GroupAction))
    # Execute the group's push/set/pop actions without running vendor code.
    for action in hardware.get_sub_entities():
        if isinstance(action, IncludeLaunchDescription):
            assert context.environment['FASTRTPS_DEFAULT_PROFILES_FILE'] == str(
                ROOT / 'malbut_bringup/config/fastdds_camera.xml')
            assert context.environment['RMW_IMPLEMENTATION'] == 'rmw_fastrtps_cpp'
            assert 'RMW_FASTRTPS_USE_QOS_FROM_XML' not in context.environment
        else:
            action.execute(context)
    assert context.environment['FASTRTPS_DEFAULT_PROFILES_FILE'] == '/existing/profile.xml'
    for action in actions:
        if isinstance(action, GroupAction) and action is not hardware:
            assert not any(isinstance(child, SetEnvironmentVariable)
                           for child in action.get_sub_entities())


def test_camera_dds_profile_keeps_udp_and_does_not_change_endpoint_qos():
    """SHM is exactly 4 MiB; default UDP discovery and endpoint policies remain."""
    import xml.etree.ElementTree as ET
    path = ROOT / 'malbut_bringup/config/fastdds_camera.xml'
    profile = ET.parse(path).getroot()
    ns = {'dds': 'http://www.eprosima.com/XMLSchemas/fastRTPS_Profiles'}
    transports = profile.findall('dds:transport_descriptors/dds:transport_descriptor', ns)
    by_type = {item.findtext('dds:type', namespaces=ns): item for item in transports}
    assert set(by_type) == {'SHM', 'UDPv4'}
    assert int(by_type['SHM'].findtext('dds:segment_size', namespaces=ns)) == 4 * 1024**2
    participant = profile.find('dds:participant', ns)
    assert participant.get('is_default_profile') == 'true'
    selected = participant.findall('dds:rtps/dds:userTransports/dds:transport_id', ns)
    assert {item.text for item in selected} == {
        item.findtext('dds:transport_id', namespaces=ns) for item in transports}
    assert participant.findtext('dds:rtps/dds:useBuiltinTransports', namespaces=ns) == 'false'
    assert profile.find('dds:publisher', ns) is None
    assert profile.find('dds:subscriber', ns) is None
    assert path.read_bytes() == (
        ROOT / 'malbut_test/malbut_bringup/config/fastdds_camera.xml').read_bytes()


def _components(context, actions):
    loader = next(item for item in actions if isinstance(item, LoadComposableNodes))
    assert loader._LoadComposableNodes__target_container == '/nav2_container'
    result = {}
    for node in loader._LoadComposableNodes__composable_node_descriptions:
        name = perform_substitutions(context, node.node_name)
        result[name] = {
            'plugin': perform_substitutions(context, node.node_plugin),
            'remappings': {perform_substitutions(context, source):
                           perform_substitutions(context, target)
                           for source, target in node.remappings or []},
            'parameters': evaluate_parameters(context, node.parameters),
        }
    return result


def test_nav2_send_buffer_profile_is_process_local(launch_module, monkeypatch):
    """Do not leak Nav2's profile to hardware, the owner, or other children."""
    from malbut_bringup import nav2_stack

    monkeypatch.setattr(nav2_stack, 'get_package_share_directory',
                        launch_module.get_package_share_directory)
    context = _context(launch_module)
    context.environment['FASTRTPS_DEFAULT_PROFILES_FILE'] = '/existing/profile.xml'
    context.environment.pop('RMW_FASTRTPS_USE_QOS_FROM_XML', None)
    before = dict(context.environment)
    actions = _core_actions(launch_module, context)
    container = _nodes(actions, 'component_container_isolated')[0]
    extra = {perform_substitutions(context, key): perform_substitutions(context, value)
             for key, value in container.process_description.additional_env}
    assert extra == {
        'RMW_IMPLEMENTATION': 'rmw_fastrtps_cpp',
        'FASTRTPS_DEFAULT_PROFILES_FILE': str(ROOT / 'malbut_bringup/config/fastdds_nav2.xml'),
    }
    assert context.environment['FASTRTPS_DEFAULT_PROFILES_FILE'] == before[
        'FASTRTPS_DEFAULT_PROFILES_FILE']
    assert 'RMW_FASTRTPS_USE_QOS_FROM_XML' not in context.environment
    for action in actions:
        if isinstance(action, Node) and action is not container:
            assert not action.process_description.additional_env


def test_nav2_profile_only_allows_send_buffer_growth_and_is_deployed():
    """Keep transports, publication mode, endpoint QoS and preallocation defaults."""
    path = ROOT / 'malbut_bringup/config/fastdds_nav2.xml'
    profile = ElementTree.parse(path).getroot()
    ns = '{http://www.eprosima.com/XMLSchemas/fastRTPS_Profiles}'
    node = profile
    for tag in ('participant', 'rtps', 'allocation', 'send_buffers', 'dynamic'):
        assert len(node) == 1
        node = node[0]
        assert node.tag == ns + tag
        if tag == 'participant':
            assert node.get('is_default_profile') == 'true'
    assert node.text == 'true'
    for relative in ('config/fastdds_nav2.xml', 'malbut_bringup/nav2_stack.py'):
        assert (ROOT / 'malbut_bringup' / relative).read_bytes() == (
            ROOT / 'malbut_test/malbut_bringup' / relative).read_bytes()


def test_missing_nav2_package_fails_the_launch_by_name(launch_module, monkeypatch):
    """Without this the lifecycle manager waits forever for the absent component."""
    from ament_index_python.packages import PackageNotFoundError
    from malbut_bringup import nav2_stack

    def share(name):
        if name == 'nav2_collision_monitor':
            raise PackageNotFoundError(name)
        return f'/opt/ros/humble/share/{name}'

    monkeypatch.setattr(nav2_stack, 'get_package_share_directory', share)
    assert nav2_stack.missing_packages() == ['nav2_collision_monitor']
    with pytest.raises(RuntimeError, match='ros-humble-nav2-collision-monitor'):
        _core_actions(launch_module, _context(launch_module))
    monkeypatch.setattr(nav2_stack, 'get_package_share_directory', lambda name: f'/x/{name}')
    assert nav2_stack.missing_packages() == []


def test_nav2_is_composed_with_collision_monitor_and_zone_filter(launch_module):
    """Navigation publishes /cmd_vel; manual driving passes the Collision Monitor."""
    context = _context(launch_module, scan_topic='/laser_raw')
    actions = _core_actions(launch_module, context)
    params = str((ROOT / 'malbut_bringup/config/nav2_params.yaml').resolve())
    container = _nodes(actions, 'component_container_isolated')
    assert len(container) == 1
    assert container[0]._Node__node_name == 'nav2_container'
    components = _components(context, actions)
    assert components['controller_server']['remappings']['cmd_vel'] == 'cmd_vel_nav'
    smoother = components['velocity_smoother']['remappings']
    assert smoother['cmd_vel'] == 'cmd_vel_nav'
    assert smoother['cmd_vel_smoothed'] == 'cmd_vel'
    assert components['behavior_server']['remappings'].get('cmd_vel', 'cmd_vel') == 'cmd_vel'
    teleop = components['teleop_behavior_server']
    assert teleop['plugin'] == 'behavior_server::BehaviorServer'
    assert teleop['remappings']['cmd_vel'] == 'cmd_vel_pre_collision'
    assert components['collision_monitor']['plugin'] == (
        'nav2_collision_monitor::CollisionMonitor')
    for name, item in components.items():
        if not name.startswith('lifecycle_manager'):
            assert str(item['parameters'][0]) == params, name
            if name == 'bt_navigator':
                directory = ROOT / 'malbut_bringup/config'
                assert item['parameters'][1] == {
                    'default_nav_to_pose_bt_xml': str(directory / 'navigate_to_pose.xml'),
                    'default_nav_through_poses_bt_xml': str(
                        directory / 'navigate_through_poses.xml'),
                }
            else:
                assert len(item['parameters']) == 1, name
            assert item['remappings']['/scan_raw'] == '/laser_raw', name
    navigation = components['lifecycle_manager_navigation']['parameters'][0]
    localization = components['lifecycle_manager_localization']['parameters'][0]
    assert navigation['autostart'] is True and localization['autostart'] is False
    assert navigation['attempt_respawn_reconnection'] is False
    assert localization['attempt_respawn_reconnection'] is False
    order = list(navigation['node_names'])
    assert order[:2] == ['zone_filter_mask_server', 'zone_filter_info_server']
    # Spinning to find the pose must not wait for the map-frame global costmap.
    for name in ('behavior_server', 'teleop_behavior_server', 'velocity_smoother',
                 'collision_monitor'):
        assert order.index(name) < order.index('planner_server'), name
    assert list(localization['node_names']) == ['map_server', 'amcl']
    assert _nodes(actions, 'zone_filter')[0].node_package == 'malbut_bringup'


def test_nav2_recovery_reloads_components_and_selected_map_without_motion(launch_module):
    """A fresh container needs fresh loaders and the current saved-map settings."""
    from geometry_msgs.msg import PoseWithCovarianceStamped
    context = _context(launch_module)
    actions = _core_actions(launch_module, context)
    container = _nodes(actions, 'component_container_isolated')[0]
    pose = PoseWithCovarianceStamped()
    pose.header.frame_id = 'map'
    pose.pose.pose.position.x = 1.5
    pose.pose.pose.orientation.w = 1.0
    reload = container._malbut_recovery_followup
    first = reload({'mode': 'LOCALIZATION', 'map': '/maps/home.yaml'}, pose)
    second = reload({'mode': 'MAPPING'}, None)
    assert first[0] is not second[0]
    saved = _components(context, first)
    assert saved['map_server']['parameters'][1]['yaml_filename'] == '/maps/home.yaml'
    assert saved['amcl']['parameters'][1]['set_initial_pose'] is True
    assert saved['amcl']['parameters'][1]['initial_pose.x'] == 1.5
    assert saved['lifecycle_manager_localization']['parameters'][0]['autostart'] is False
    assert saved['lifecycle_manager_navigation']['parameters'][0]['autostart'] is False
    mapping = _components(context, second)
    assert mapping['lifecycle_manager_localization']['parameters'][0]['autostart'] is False
    groups = container._malbut_recovery_lifecycle(
        {'mode': 'LOCALIZATION', 'map': '/maps/home.yaml'}, pose)
    assert [group['manager'] for group in groups] == [
        'lifecycle_manager_localization', 'lifecycle_manager_navigation']
    assert groups[0]['parameters']['map_server']['yaml_filename'] == '/maps/home.yaml'
    assert groups[0]['parameters']['amcl']['initial_pose.x'] == 1.5
    groups = container._malbut_recovery_lifecycle({'mode': 'MAPPING'}, None)
    assert [group['manager'] for group in groups] == ['lifecycle_manager_navigation']


def test_general_navigation_trees_select_the_position_only_checker():
    """Two installed checkers must not make ordinary FollowPath goals ambiguous."""
    for name in ('navigate_to_pose.xml', 'navigate_through_poses.xml'):
        tree = ElementTree.parse(ROOT / 'malbut_bringup/config' / name)
        goals = list(tree.iter('FollowPath'))
        assert len(goals) == 1
        assert goals[0].attrib['controller_id'] == 'FollowPath'
        assert goals[0].attrib['goal_checker_id'] == 'general_goal_checker'


def test_nav2_preserves_custom_navigation_trees(launch_module, tmp_path):
    """A supplied tree must not be replaced by the default-checker wiring."""
    import yaml
    source = ROOT / 'malbut_bringup/config/nav2_params.yaml'
    config = yaml.safe_load(source.read_text())
    bt = config['bt_navigator']['ros__parameters']
    bt['default_nav_to_pose_bt_xml'] = '/custom/to-pose.xml'
    bt['default_nav_through_poses_bt_xml'] = '/custom/through-poses.xml'
    params = tmp_path / 'nav2.yaml'
    params.write_text(yaml.safe_dump(config))
    context = _context(launch_module, nav2_params_file=str(params))
    components = _components(context, _core_actions(launch_module, context))
    assert len(components['bt_navigator']['parameters']) == 1


@pytest.fixture
def fall_config(tmp_path):
    """Use test-only limits; never create a key, database or Cloud connection."""
    path = tmp_path / 'fall settings.json'
    path.write_text(json.dumps({
        'device_id': 'test-robot', 'journal_path': str(tmp_path / 'journal.sqlite'),
        'cloud_key_file': str(tmp_path / 'not-read.key'), 'model': 'gemma4:31b',
        'image_topic': '/configured/rgb', 'retention_s': 10,
        'buffer_bytes': 10000000, 'buffer_frames': 100, 'input_fps': 5,
        'max_source_age_s': 1, 'control_lease_s': 5,
        'policy': {'retry_interval_s': 3, 'max_person_observation_age_s': 2,
                   'clip_window_s': 5, 'max_frame_age_s': 2, 'max_calls_per_minute': 5,
                   'max_incidents': 10, 'max_images': 12},
    }))
    return path


def test_robot_contains_only_shared_nodes_and_no_external_readiness_gate(launch_module):
    context = _context(launch_module)
    actions = launch_module._setup(context)
    assert {item.node_executable for item in actions if isinstance(item, Node)} == {
        'component_container_isolated', 'zone_filter', 'system_manager'}
    assert len(_includes(actions)) == 1
    hardware = dict(_includes(actions)[0].launch_arguments)
    assert hardware['robot_name'] == hardware['master_name'] == '/'
    assert hardware['point_cloud_enable'] == hardware['use_joy'] == 'false'
    manager = _parameters(context, _nodes(actions, 'system_manager')[0])
    assert manager['ready_topic'] == ''
    assert manager['localization_control'] is True
    assert manager['relocalize_action'] == '/relocalize'
    assert manager['initial_map'] == str(ROOT / 'malbut_bringup/config/default_map.yaml')
    assert manager['default_map'] == manager['initial_map']


def test_default_map_contains_only_unknown_cells_and_is_deployed_identically():
    """Use a standard map asset, without a separate mapless Nav2 profile."""
    import yaml

    source = ROOT / 'malbut_bringup/config'
    deployed = ROOT / 'malbut_test/malbut_bringup/config'
    config = yaml.safe_load((source / 'default_map.yaml').read_text())
    lines = (source / config['image']).read_text().splitlines()
    tokens = ' '.join(line for line in lines if not line.startswith('#')).split()
    assert tokens[0] == 'P2'
    width, height, maximum = map(int, tokens[1:4])
    pixels = list(map(int, tokens[4:]))
    assert (width, height) == (400, 400)
    assert len(pixels) == width * height and set(pixels) == {128}
    assert config['free_thresh'] < 1.0 - pixels[0] / maximum < config['occupied_thresh']
    assert config['mode'] == 'trinary' and config['negate'] == 0
    assert config['resolution'] == 0.05 and config['origin'] == [-10.0, -10.0, 0.0]
    for name in ('default_map.yaml', config['image']):
        assert (source / name).read_bytes() == (deployed / name).read_bytes()


def test_aggregate_schedules_modules_with_only_a_read_only_observer(launch_module, fall_config):
    module = _load('bringup')
    context = _context(module, fall_monitor='true', fall_config=str(fall_config))
    actions = module._setup(context)
    includes = _included_modules(actions)
    assert set(includes) == {
        'robot', 'tracking', 'patrol', 'autoslam', 'manual', 'relocalization', 'fall', 'speech'}
    observer, = _nodes(actions, 'wait_for_robot')
    observed = _parameters(context, observer)
    assert observed['observe_only'] is True and observed['speech'] is True
    assert 'slam_toolbox' not in observed['startup_nodes']
    assert '/autoslam' in observed['required_actions'].split(',')
    assert '/navigate_to_pose' in observed['required_actions'].split(',')
    assert not any(type(item).__name__ in ('TimerAction', 'RegisterEventHandler')
                   for item in actions)
    assert 'control_server' not in includes['speech']
    assert all(item['use_sim_time'] == 'false' for item in includes.values())
    assert context.locals.malbut_modular_bringup


def test_optional_modules_can_all_be_disabled(launch_module):
    module = _load('bringup')
    context = _context(module, perception='false', patrol='false', autoslam='false',
                       manual='false', relocalization='false', fall_monitor='false',
                       homecam='false', speech='false')
    includes = _included_modules(module._setup(context))
    assert set(includes) == {'robot'}
    assert includes['robot']['restore_pose'] == 'false'
    observer, = _nodes(module._setup(context), 'wait_for_robot')
    assert _parameters(context, observer)['speech'] is False


def test_selected_map_and_explicit_pose_choice_reach_common_manager(launch_module, tmp_path):
    path = tmp_path / 'real map.yaml'
    path.write_text('image: real.png\n')
    context = _context(launch_module, map=str(path), restore_pose='false')
    params = _parameters(context, _nodes(launch_module._setup(context), 'system_manager')[0])
    assert params['initial_map'] == str(path)
    assert params['relocalize_action'] == ''
    with pytest.raises(RuntimeError, match='saved map YAML'):
        launch_module._setup(_context(launch_module, map='/missing.yaml'))


@pytest.mark.parametrize('name', [
    'robot', 'tracking', 'patrol', 'autoslam', 'manual', 'relocalization', 'homecam', 'fall'])
def test_standalone_module_constructs_without_any_running_ros_peer(
        launch_module, fall_config, name):
    module = _load(name)
    context = _context(module, fall_monitor='true', fall_config=str(fall_config))
    context.environment['HOMECAM_BACKEND_URL'] = 'https://robot.example.com'
    actions = module._setup(context)
    assert actions
    assert not _nodes(actions, 'wait_for_robot')
    assert not any(isinstance(action, ExecuteProcess) and not isinstance(action, Node)
                   for action in actions)


def test_missing_optional_model_does_not_prevent_other_modules(launch_module, fall_config):
    top = _load('bringup')
    context = _context(top, python_executable='/missing/yolo',
                       fall_monitor='true', fall_config=str(fall_config),
                       fall_pose_model_path='/missing/pose')
    scheduled = _included_modules(top._setup(context))
    assert {'tracking', 'fall', 'speech', 'robot'} <= scheduled.keys()
    for name in ('tracking', 'fall'):
        result = _module_setup(name, context)
        assert len(result) == 1 and isinstance(result[0], LogInfo)
        assert context.locals.malbut_modules[name]
    for name in ('robot', 'patrol', 'autoslam'):
        assert _module_setup(name, context)
        assert context.locals.malbut_modules[name] == ''


def test_standalone_configuration_failure_is_reported(launch_module):
    context = _context(_load('tracking'), python_executable='/missing/python')
    with pytest.raises(RuntimeError, match='Perception files'):
        _module_setup('tracking', context)


def test_module_error_does_not_shutdown_other_launch_process(tmp_path):
    marker = tmp_path / 'alive'

    def broken(_context):
        raise RuntimeError('model missing')

    def healthy(_context):
        return [ExecuteProcess(cmd=['/usr/bin/touch', str(marker)])]

    def bind(context):
        context.extend_globals({'malbut_modular_bringup': True})
        return []
    service = LaunchService()
    service.include_launch_description(LaunchDescription([
        OpaqueFunction(function=bind),
        launch_support.module_actions('broken', broken),
        launch_support.module_actions('healthy', healthy),
    ]))
    assert service.run() == 0
    assert marker.is_file()


def test_tracking_reuses_detection_and_following_without_shared_drivers(launch_module):
    module = _load('tracking')
    context = _context(module, scan_topic='/laser', rgb_topic='/camera/color')
    includes = [dict(item.launch_arguments) for item in _includes(module._setup(context))]
    assert len(includes) == 2
    detection, follower = includes
    assert detection['rgb_topic'] == '/camera/color'
    assert detection['reid_backend'] == 'osnet'
    assert follower['scan_topic'] == '/laser'
    assert follower['lidar_config'].endswith('malbut_tracking/config/lidar_foreground.yaml')
    assert 'config' not in follower  # Never shadow the child's YAML default with ''.


def test_application_topic_wiring_and_manual_input_are_preserved(launch_module):
    module = _load('patrol')
    context = _context(module, static_map_topic='/mapping/map', robot_frame='robot/base',
                       rgb_topic='/camera/color')
    options = dict(_includes(module._setup(context))[0].launch_arguments)
    assert options['map_topic'] == '/mapping/map'
    assert options['base_frame'] == 'robot/base'
    assert options['camera_image_topic'] == '/camera/color'
    module = _load('autoslam')
    context = _context(module, map_directory='/maps', robot_frame='robot/base')
    options = dict(_includes(module._setup(context))[0].launch_arguments)
    assert options['map_directory'] == '/maps' and options['base_frame'] == 'robot/base'
    assert options['start_mapping_service'] == '/malbut/localization/start_mapping'
    assert options['stop_mapping_service'] == '/malbut/localization/stop_mapping'
    module = _load('manual')
    context = _context(module)
    actions = module._setup(context)
    assert _parameters(context, _nodes(actions, 'manual_control')[0])['teleop_topic'] == (
        '/cmd_vel_teleop')
    joystick = _nodes(actions, 'joystick_control')[0]
    assert _parameters(context, joystick)['max_linear'] == 0.15
    assert [(perform_substitutions(context, a), perform_substitutions(context, b))
            for a, b in joystick._Node__remappings] == [
                ('controller/cmd_vel', '/cmd_vel_teleop')]
    assert not _nodes(module._setup(_context(module, start_hardware='false')), 'joystick_control')


def test_media_and_fall_share_session_ids_without_start_order(launch_module, fall_config):
    from uuid import UUID
    top = _load('bringup')
    previous = None
    for _ in range(2):
        context = _context(top, speech='false', fall_config=str(fall_config))
        context.environment['HOMECAM_BACKEND_URL'] = 'https://robot.example.com'
        modules = _included_modules(top._setup(context))
        for peer in ('manager', 'bridge', 'vlm'):
            key = 'fall_' + peer + '_runtime_id'
            assert UUID(modules['fall'][key])
            assert modules['fall'][key] == modules['homecam'][key]
        assert modules['fall']['fall_bridge_runtime_id'] != previous
        previous = modules['fall']['fall_bridge_runtime_id']
        context.launch_configurations.update(modules['fall'])
        actions = _load('fall')._setup(context)
        coordinator = _parameters(context, _nodes(actions, 'fall_coordinator')[0])
        monitor = _parameters(context, _nodes(actions, 'malbut-fall-monitor')[0])
        pose = _parameters(context, _nodes(actions, 'homecam_detector_node')[0])
        assert coordinator['runtime_id'] == monitor['manager_runtime_id']
        assert coordinator['vlm_runtime_id'] == monitor['runtime_id'] == pose['fall_runtime_id']


def test_fall_starts_one_non_ros_uploader_with_shared_settings(launch_module, fall_config):
    module = _load('fall')
    context = _context(module, fall_config=str(fall_config))
    context.environment.update(HOMECAM_BACKEND_URL='https://robot.example.com',
                               HOMECAM_DEVICE_TOKEN_FILE='/protected/device.token',
                               HOMECAM_DEVICE_ID='test-robot')
    actions = module._setup(context)
    workers = [a for a in actions if isinstance(a, ExecuteProcess) and not isinstance(a, Node)]
    assert len(workers) == 1
    worker = workers[0]
    args = [perform_substitutions(context, part) for part in worker.cmd[1:]]
    assert args[args.index('--journal') + 1] == json.loads(fall_config.read_text())['journal_path']
    assert args[args.index('--device-id') + 1] == 'test-robot'
    assert args[args.index('--base-url') + 1] == 'https://robot.example.com'
    assert '--execute' in args and '--upload-clips' in args
    assert not {'--once', '--ros-args', '--params-file', '--retry-auth-failed'} & set(args)
    from launch_ros.substitutions import ExecutableInPackage
    assert isinstance(worker.cmd[0][0], ExecutableInPackage)
    assert worker._ExecuteLocal__respawn_delay == 5.0
    assert worker._ExecuteLocal__respawn is True
    assert len(_nodes(actions, 'malbut-fall-monitor')) == 1
    assert len(_nodes(actions, 'homecam_detector_node')) == 1


@pytest.mark.parametrize('environment', [
    {}, {'HOMECAM_BACKEND_URL': 'https://robot.example.com'},
    {'HOMECAM_BACKEND_URL': 'http://robot.example.com',
     'HOMECAM_DEVICE_TOKEN_FILE': '/protected/device.token'},
    {'HOMECAM_BACKEND_URL': 'https://robot.example.com',
     'HOMECAM_DEVICE_TOKEN_FILE': '/protected/device.token', 'HOMECAM_DEVICE_ID': 'wrong'},
])
def test_upload_setup_failure_does_not_disable_detection(launch_module, fall_config, environment):
    module = _load('fall')
    context = _context(module, fall_config=str(fall_config))
    context.environment.update(environment)
    actions = module._setup(context)
    assert {a.node_executable for a in actions if isinstance(a, Node)} == {
        'malbut-fall-monitor', 'homecam_detector_node', 'fall_coordinator', 'fall_approach'}
    assert not any(isinstance(a, ExecuteProcess) and not isinstance(a, Node) for a in actions)
    assert any(isinstance(a, LogInfo) for a in actions)


def test_disabled_fall_never_starts_uploader(launch_module, fall_config):
    module = _load('fall')
    context = _context(module, fall_monitor='false', fall_config=str(fall_config))
    context.environment.update(HOMECAM_BACKEND_URL='https://robot.example.com',
                               HOMECAM_DEVICE_TOKEN_FILE='/protected/device.token')
    assert not any(isinstance(a, ExecuteProcess) for a in module._setup(context))


def test_upload_process_restarts_and_stops_with_launch_without_ros_arguments(
        launch_module, fall_config, tmp_path, monkeypatch):
    """Real launch child and real worker; fake HTTPS ack, no robot/VLM/server."""
    from types import SimpleNamespace
    from launch.actions import EmitEvent, RegisterEventHandler, TimerAction
    from launch.event_handlers import OnProcessExit
    from launch.events import Shutdown
    from launch_ros.substitutions import ExecutableInPackage
    from malbut_agent_server.adapters.outbound.sqlite_fall_journal import SqliteFallJournal
    from malbut_agent_server.fall_upload_worker import _upload_lock

    data = json.loads(fall_config.read_text())
    journal_path = tmp_path / 'private/events.sqlite'
    data['journal_path'] = str(journal_path)
    fall_config.write_text(json.dumps(data))
    with_journal = SqliteFallJournal(journal_path, device_id='test-robot')
    try:
        with_journal.append(
            device_id='test-robot', boot_id='test-boot',
            event=SimpleNamespace(incident_id='incident-test', event_id='event-test',
                                  kind='incident_opened', reason=None, notification_level=None),
            incident=SimpleNamespace(
                incident_id='incident-test', revision=1,
                state=SimpleNamespace(value='verifying'), fall_seen=False,
                video=None, answer=None))
    finally:
        with_journal.close()
    token = tmp_path / 'device.token'
    token.write_text('test-device-token')
    token.chmod(0o600)
    first = tmp_path / 'first-launch'
    received = tmp_path / 'received'
    child = tmp_path / 'upload-test-entrypoint'
    child.write_text(f'''#!{sys.executable}
import io, json, sys, urllib.request
from pathlib import Path
from malbut_agent_server import fall_upload_worker as worker
first = Path({str(first)!r})
if not first.exists():
    first.touch()
    raise SystemExit(2)  # Simulated first startup failure, must respawn.
def server(self, request, **kwargs):
    payload = json.loads(request.data)
    Path({str(received)!r}).write_text(payload['eventId'])
    response = io.BytesIO(json.dumps(dict(stored=True, eventId=payload['eventId'])).encode())
    response.status = 201
    return response
urllib.request.OpenerDirector.open = server
def stop_when_idle(_):
    raise KeyboardInterrupt
worker.time.sleep = stop_when_idle
raise SystemExit(worker.main())
''')
    child.chmod(0o700)
    monkeypatch.setattr(ExecutableInPackage, 'perform', lambda self, context: str(child))
    monkeypatch.setenv('ROS_LOG_DIR', str(tmp_path / 'ros-log'))
    module = _load('fall')
    context = _context(module, fall_config=str(fall_config))
    context.environment.update(HOMECAM_BACKEND_URL='https://web.example.com',
                               HOMECAM_DEVICE_TOKEN_FILE=str(token))
    worker, = [a for a in module._setup(context)
               if isinstance(a, ExecuteProcess) and not isinstance(a, Node)]
    service = LaunchService()
    service.include_launch_description(LaunchDescription([
        RegisterEventHandler(OnProcessExit(
            target_action=worker,
            on_exit=lambda event, context: [EmitEvent(event=Shutdown(reason='test completed'))]
            if event.returncode == 0 else [])),
        worker,
        TimerAction(period=15.0, actions=[EmitEvent(event=Shutdown(reason='test timeout'))]),
    ]))
    assert service.run() == 0
    assert first.exists() and received.read_text() == 'event-test'
    journal = SqliteFallJournal(journal_path, device_id='test-robot')
    try:
        assert journal.upload_status()[0]['status'] == 'stored'
    finally:
        journal.close()
    with _upload_lock(journal_path):
        pass  # Child shutdown released the lock; no orphaned sender remains.


@pytest.mark.parametrize('cuda_mode', [None, 0o644, 0o755])
def test_fall_python_uses_dedicated_runtime_even_with_legacy_cuda_dir(
        tmp_path, monkeypatch, cuda_mode):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path))
    monkeypatch.delenv('MALBUT_FALL_POSE_PYTHON', raising=False)
    cuda_python = tmp_path / 'malbut_fall_pose/runtime-cuda/bin/python'
    if cuda_mode is not None:
        cuda_python.parent.mkdir(parents=True)
        cuda_python.write_text('#!/bin/sh\nexit 0\n')
        cuda_python.chmod(cuda_mode)
    expected = tmp_path / 'malbut_fall_pose/runtime/bin/python'
    for name in ('fall', 'bringup'):
        module = _load(name)
        context = _context(module)
        assert context.launch_configurations['fall_pose_python_executable'] == str(expected)
        assert context.launch_configurations['fall_pose_execution_provider'] == 'auto'
        context = _context(module, fall_pose_python_executable='/explicit/python')
        assert context.launch_configurations['fall_pose_python_executable'] == '/explicit/python'
    monkeypatch.setenv('MALBUT_FALL_POSE_PYTHON', '/custom/python')
    assert launch_support.defaults()['fall_pose_python_executable'] == '/custom/python'


@pytest.mark.parametrize('overrides,expected', [
    ({}, ('auto', 2, False, 1)),
    ({'fall_pose_execution_provider': 'cpu', 'fall_pose_intra_op_num_threads': '0',
      'fall_pose_allow_spinning': 'true', 'fall_pose_opencv_num_threads': '0'},
     ('cpu', 0, True, 0)),
    ({'fall_pose_execution_provider': 'cuda'}, ('cuda', 2, False, 1)),
])
def test_fall_pose_keeps_robot_defaults(launch_module, fall_config, overrides, expected):
    module = _load('fall')
    context = _context(module, fall_config=str(fall_config), **overrides)
    params = _parameters(context, _nodes(module._setup(context), 'homecam_detector_node')[0])
    assert (params['pose_execution_provider'], params['pose_intra_op_num_threads'],
            params['pose_allow_spinning'], params['pose_opencv_num_threads']) == expected
    assert params['pose_keep_aspect'] is True and params['pose_inference_fps'] == 5.0
    assert 'fall_only' not in params


@pytest.mark.parametrize('changes', [
    {'fall_pose_execution_provider': 'invalid'}, {'fall_pose_intra_op_num_threads': '-1'},
    {'fall_pose_allow_spinning': 'maybe'}, {'fall_pose_opencv_num_threads': '-1'},
    {'fall_pose_model_path': '/missing/pose.onnx'},
])
def test_fall_rejects_invalid_local_setup(launch_module, fall_config, changes):
    module = _load('fall')
    with pytest.raises(RuntimeError):
        context = _context(module, fall_config=str(fall_config), **changes)
        module._setup(context)


def test_cloud_media_uses_existing_camera_without_duplication(launch_module):
    module = _load('homecam')
    context = _context(module, rgb_topic='/rgb', camera_info_topic='/info', odom_topic='/wheel')
    context.environment['HOMECAM_BACKEND_URL'] = 'https://robot.example.com'
    context.environment['HOMECAM_DEVICE_ID'] = 'robot-1'
    actions = module._setup(context)
    assert not any(isinstance(item, Node) for item in actions)
    media = dict(_includes(actions)[0].launch_arguments)
    assert media['backend_url'] == 'https://robot.example.com'
    assert media['device_id'] == 'robot-1'
    assert (media['image_topic'], media['camera_info_topic'], media['odom_topic']) == (
        '/rgb', '/info', '/wheel')


def test_shared_microphone_prepared_once_and_only_for_audio_modules(launch_module, monkeypatch):
    module = _load('bringup')
    calls = []
    monkeypatch.setattr(module, 'shared_xfm_source', lambda env: calls.append(env) or 'xfm-source')
    context = _context(module)
    context.environment['HOMECAM_BACKEND_URL'] = 'https://robot.example.com'
    actions = module._setup(context)
    assert len(calls) == 1
    for group in [item for item in actions if isinstance(item, GroupAction)]:
        for action in group.get_sub_entities():
            if isinstance(action, IncludeLaunchDescription):
                name = Path(_source_path(action)).name
                if name in ('homecam.launch.py', 'speech.launch.py'):
                    assert context.environment['MALBUT_SHARED_MICROPHONE'] == 'xfm-source'
                    assert context.environment['PULSE_SOURCE'] == 'xfm-source'
                else:
                    assert 'MALBUT_SHARED_MICROPHONE' not in context.environment
            else:
                action.execute(context)
    assert 'MALBUT_SHARED_MICROPHONE' not in context.environment


def test_shared_microphone_failure_is_confined_to_audio_modules(launch_module, monkeypatch):
    module = _load('bringup')

    def missing(_):
        raise RuntimeError('Shared microphone unavailable')
    monkeypatch.setattr(module, 'shared_xfm_source', missing)
    context = _context(module)
    context.environment['HOMECAM_BACKEND_URL'] = 'https://robot.example.com'
    includes = _included_modules(module._setup(context))
    assert 'robot' in includes and 'tracking' in includes
    assert 'speech' not in includes and 'homecam' not in includes
    assert set(context.locals.malbut_modules) == {'homecam', 'speech'}


def test_runtime_dependencies_do_not_pull_simulation_or_new_hardware_package():
    package = ElementTree.parse(ROOT / 'malbut_bringup/package.xml').getroot()
    dependencies = {item.text for item in package.findall('exec_depend')}
    assert {'malbut_tracking', 'malbut_patrol', 'malbut_system_manager',
            'homecam_media_agent'} <= dependencies
    assert not {'malbut_gazebo', 'malbut_scenarios', 'malbut_hardware'} & dependencies
