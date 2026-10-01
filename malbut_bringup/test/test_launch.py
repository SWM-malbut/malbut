"""Check composition without launching hardware, inference, or navigation."""

import importlib.util
import json
from pathlib import Path
import sys
from xml.etree import ElementTree

from launch import LaunchContext, LaunchDescription, LaunchService
from launch.actions import (
    DeclareLaunchArgument, EmitEvent, ExecuteProcess, GroupAction, IncludeLaunchDescription,
    RegisterEventHandler, SetEnvironmentVariable,
)
from launch.event_handlers import OnProcessExit
from launch.events import Shutdown
from launch.events.process import ProcessExited
from launch.utilities import perform_substitutions
from launch_ros.actions import LoadComposableNodes, Node, SetParameter
from launch_ros.utilities import evaluate_parameters
import pytest


ROOT = Path(__file__).parents[2]


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


@pytest.fixture
def launch_module(tmp_path, monkeypatch):
    """Supply fake vendor assets, but use the real Malbut launch files."""
    monkeypatch.delenv('HOMECAM_BACKEND_URL', raising=False)
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
    monkeypatch.setattr(module.Path, 'home', lambda: tmp_path / 'home')
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

    module.get_package_share_directory = package_share
    return module


def _context(module, **overrides):
    context = LaunchContext()
    # These legacy cases exercise the hardware graph independently of speech.
    context.launch_configurations['speech'] = 'false'
    context.launch_configurations.update(overrides)
    for action in module.generate_launch_description().entities:
        if isinstance(action, DeclareLaunchArgument):
            action.execute(context)
    return context


def _includes(actions):
    return [child for action in actions if isinstance(action, GroupAction)
            for child in action.get_sub_entities()
            if isinstance(child, IncludeLaunchDescription)]


def _joystick_nodes(actions):
    return [item for item in actions if isinstance(item, Node)
            and item.node_executable == 'joystick_control']


def _readiness_exit(actions, context, returncode=0):
    wait = next(item for item in actions if isinstance(item, Node)
                and item.node_executable == 'wait_for_robot')
    event = ProcessExited(action=wait, name='bringup_readiness', cmd=[],
                          cwd=None, env=None, pid=1, returncode=returncode)
    result = []
    for registration in actions:
        if isinstance(registration, RegisterEventHandler):
            handler = registration.event_handler
            if handler.matches(event):
                result.extend(handler.handle(event, context) or [])
    return result


def _nodes(actions, executable):
    return [item for item in actions if isinstance(item, Node)
            and item.node_executable == executable]


def _parameters(context, node):
    return evaluate_parameters(context, node._Node__parameters)[0]


def _core_actions(launch_module, context):
    """Inspect the core graph by completing only its earlier startup probes."""
    actions = launch_module._setup(context)
    while True:
        gate = next(item for item in actions if isinstance(item, Node)
                    and item.node_executable == 'wait_for_robot')
        if _parameters(context, gate)['startup_stage'] == 'applications':
            return actions
        following = _process_exit(actions, context, gate)
        actions = [item for item in actions if item is not gate] + following


def test_groups_start_only_after_previous_probe_and_count_enabled_stages(
        launch_module, speech_assets, fall_config):
    context = _context(launch_module, **speech_assets, fall_monitor='true',
                       fall_config=str(fall_config))
    initial = launch_module._setup(context)
    current = initial
    assert not _nodes(initial, 'system_manager')
    assert len(_includes(initial)) == 1  # Only the vendor hardware group.
    expected = ['sensors', 'perception', 'navigation', 'following', 'patrol',
                'applications', 'extensions', 'speech']
    for index, kind in enumerate(expected, start=1):
        gate = _nodes(current, 'wait_for_robot')[0]
        params = _parameters(context, gate)
        assert (params['startup_stage'], params['startup_index'], params['startup_total']) == (
            kind, index, len(expected))
        if kind == 'navigation':
            assert _nodes(current, 'system_manager')
            assert _nodes(current, 'relocalization')
            assert _nodes(current, 'component_container_isolated')
            assert not _includes(current)  # Applications have not started.
        if kind == 'extensions':
            assert _nodes(current, 'homecam_detector_node')
            assert not any('stt_model_path' in dict(item.launch_arguments)
                           for item in _includes(current))
        current = _process_exit(initial, context, gate)
        assert _process_exit(initial, context, gate) == []  # No duplicate startup.
    assert not _nodes(current, 'wait_for_robot')


def test_stage_failure_and_shutdown_do_not_start_later_groups(launch_module):
    context = _context(launch_module, perception='false')
    initial = launch_module._setup(context)
    gate = _nodes(initial, 'wait_for_robot')[0]
    assert _parameters(context, gate)['startup_total'] == 4
    with pytest.raises(RuntimeError, match='Bringup stage failed'):
        _process_exit(initial, context, gate, returncode=1)
    context._set_is_shutdown(True)
    assert _process_exit(initial, context, gate) == []


def test_one_bringup_starts_everything_and_maps_without_a_saved_map(launch_module):
    """No modes: hardware, Nav2, applications and the manager always start."""
    context = _context(launch_module)
    actions = _core_actions(launch_module, context)
    options = [dict(item.launch_arguments) for item in _includes(actions)]
    # Hardware, person detection and following, patrol and AutoSLAM; Nav2 is composed.
    assert len(options) == 5
    assert all(item['use_sim_time'] == 'false' for item in options)
    hardware = options[0]
    assert hardware['sim'] == 'false'
    assert hardware['robot_name'] == hardware['master_name'] == '/'
    assert hardware['point_cloud_enable'] == 'false'
    # The vendor joystick becomes manual_drive input instead of a second /cmd_vel source.
    assert hardware['use_joy'] == 'false'
    joystick = _nodes(actions, 'joystick_control')
    assert len(joystick) == 1 and joystick[0].node_package == 'peripherals'
    assert [(str(source[0].perform(context)), str(target[0].perform(context)))
            for source, target in joystick[0]._Node__remappings] == [
        ('controller/cmd_vel', '/cmd_vel_teleop')]
    assert _parameters(context, joystick[0])['max_linear'] == 0.15
    assert _parameters(context, joystick[0])['max_angular'] == 0.45
    assert sum('reid_backend' in item for item in options) == 1
    assert sum('lidar_config' in item for item in options) == 1
    assert sum('camera_image_topic' in item for item in options) == 1
    manager = _parameters(context, _nodes(actions, 'system_manager')[0])
    assert manager['localization_control'] is True
    assert manager['initial_map'] == ''
    assert manager['ready_topic'] == '/malbut/bringup/status'
    assert manager['slam_params_file'].endswith('malbut_bringup/config/slam_toolbox.yaml')
    assert _parameters(context, _nodes(actions, 'manual_control')[0])[
        'teleop_topic'] == '/cmd_vel_teleop'
    # Each saved-map load finds the robot through the relocalization Action.
    assert _nodes(actions, 'relocalization')[0].node_package == 'malbut_relocalization'
    assert manager['relocalize_action'] == '/relocalize'
    assert _parameters(context, _nodes(actions, 'wait_for_robot')[0])['relocalization'] is True


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
            assert [str(path) for path in item['parameters']] == [params], name
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


@pytest.mark.parametrize('options', [{'relocalization': 'false'}, {'restore_pose': 'false'}])
def test_pose_finding_can_be_left_to_the_operator(launch_module, options):
    """Without it the manager loads maps and the operator sets the pose."""
    context = _context(launch_module, **options)
    actions = _core_actions(launch_module, context)
    manager = _parameters(context, _nodes(actions, 'system_manager')[0])
    assert manager['relocalize_action'] == ''
    assert bool(_nodes(actions, 'relocalization')) == ('relocalization' not in options)


def test_reused_hardware_is_not_launched_again(launch_module):
    """Externally started drivers keep their own joystick; no duplicates start."""
    context = _context(launch_module, start_hardware='false', perception='false')
    actions = _core_actions(launch_module, context)
    assert not any('robot_name' in dict(item.launch_arguments) for item in _includes(actions))
    assert not _nodes(actions, 'joystick_control')
    assert not any('reid_backend' in dict(item.launch_arguments)
                   for item in _includes(actions))
    wait = _parameters(context, _nodes(actions, 'wait_for_robot')[0])
    assert wait['navigation'] is True and wait['perception'] is False


def test_applications_and_panel_receive_robot_topics(launch_module):
    """Configured topics and frames reach autoslam, the panel and readiness."""
    context = _context(launch_module, web_panel='true', scan_topic='/laser_raw',
                       map_directory='/configured/maps', static_map_topic='/mapping/map',
                       robot_frame='robot/base', rgb_topic='/camera/color')
    actions = _core_actions(launch_module, context)
    options = [dict(item.launch_arguments) for item in _includes(actions)]
    autoslam = next(item for item in options if 'map_directory' in item)
    assert autoslam['map_directory'] == '/configured/maps'
    assert autoslam['map_topic'] == '/mapping/map'
    assert autoslam['base_frame'] == 'robot/base'
    assert _parameters(context, _nodes(actions, 'robot_web_panel')[0]) == {
        'use_sim_time': False, 'manage_bringup': False,
        'map_directory': '/configured/maps', 'map_topic': '/global_costmap/costmap',
        'robot_frame': 'robot/base', 'rgb_topic': '/camera/color',
    }
    assert _parameters(context, _nodes(actions, 'system_manager')[0])[
        'scan_topic'] == '/laser_raw'
    for group in [item for item in actions if isinstance(item, GroupAction)]:
        # A global -p creates /** before the named config. In rclcpp this can
        # make that config's /scan beat the later inline /scan_raw.
        assert not any(isinstance(child, SetParameter) for child in group.get_sub_entities())


def test_cloud_bringup_includes_one_media_sender_on_real_topics(launch_module):
    """The bridge stays separate; media follows the Bringup lifetime."""
    context = _context(launch_module, start_hardware='false', rgb_topic='/camera/color',
                       camera_info_topic='/camera/info', odom_topic='/robot/odom')
    context.environment['HOMECAM_BACKEND_URL'] = 'https://robot.example.com'
    context.environment['HOMECAM_DEVICE_ID'] = 'robot-1'
    actions = _core_actions(launch_module, context)
    options = [dict(item.launch_arguments)
               for item in _includes(_readiness_exit(actions, context))]
    media = [item for item in options if 'backend_url' in item]
    assert media == [{
        'backend_url': 'https://robot.example.com', 'device_id': 'robot-1',
        'image_topic': '/camera/color', 'camera_info_topic': '/camera/info',
        'odom_topic': '/robot/odom', 'use_sim_time': 'false',
    }]


def test_missing_perception_files_fail_before_constructing_hardware(launch_module):
    """Report missing inference setup before any child launch can be returned."""
    context = _context(launch_module, python_executable='/not/prepared')
    launch_module._include = lambda *_args, **_kwargs: pytest.fail('child constructed')
    with pytest.raises(RuntimeError, match='Perception files are not ready'):
        _core_actions(launch_module, context)


def test_selected_map_starts_saved_map_localization(launch_module, tmp_path):
    """A given map picks the first localization; a missing one fails early."""
    map_path = tmp_path / 'real house.yaml'
    map_path.write_text('image: real_house.pgm\n')
    context = _context(launch_module, map=str(map_path))
    actions = _core_actions(launch_module, context)
    manager = _parameters(context, _nodes(actions, 'system_manager')[0])
    assert manager['initial_map'] == str(map_path)
    with pytest.raises(RuntimeError, match='saved map YAML'):
        _core_actions(launch_module, _context(launch_module, map=str(tmp_path / 'missing.yaml')))


@pytest.mark.parametrize('returncode', [0, 1])
def test_manager_starts_first_and_missions_wait_for_readiness(launch_module, returncode):
    """Localization must precede Nav2 activation; readiness only opens missions."""
    context = _context(launch_module, start_hardware='false')
    actions = _core_actions(launch_module, context)
    assert len(_nodes(actions, 'system_manager')) == 1
    if returncode:
        with pytest.raises(RuntimeError, match='Bringup stage failed'):
            _readiness_exit(actions, context, returncode=returncode)
    else:
        started = _readiness_exit(actions, context)
        assert not any(isinstance(item, Node) for item in started)


def test_runtime_dependencies_do_not_pull_simulation_or_new_hardware_package():
    """Vendor packages are runtime prerequisites, not fabricated rosdep keys."""
    package = ElementTree.parse(ROOT / 'malbut_bringup/package.xml').getroot()
    dependencies = {item.text for item in package.findall('exec_depend')}
    assert {'malbut_tracking', 'malbut_patrol', 'malbut_system_manager',
            'homecam_media_agent'} <= dependencies
    assert not {'malbut_gazebo', 'malbut_scenarios', 'malbut_hardware'} & dependencies


@pytest.fixture
def speech_assets(launch_module, tmp_path):
    """Prepare only path fixtures; these tests never load a model or audio."""
    runtime = tmp_path / 'speech runtime'
    python = runtime / 'bin/python'
    python.parent.mkdir(parents=True)
    python.symlink_to('/bin/sh')
    model = tmp_path / 'model.bin'
    library = tmp_path / 'bridge.so'
    model.touch()
    library.touch()
    return {
        'speech': 'true', 'speech_python_executable': str(python),
        'stt_model_path': str(model), 'stt_library_path': str(library),
    }


def _process_exit(actions, context, process, returncode=0):
    event = ProcessExited(action=process, name='test_child', cmd=[],
                          cwd=None, env=None, pid=1, returncode=returncode)
    result = []
    for action in actions:
        if isinstance(action, RegisterEventHandler):
            handler = action.event_handler
            if handler.matches(event):
                result.extend(handler.handle(event, context) or [])
    return result


def test_cloud_and_stt_share_xfm_before_capture_starts(
        launch_module, speech_assets, monkeypatch):
    """Both clients get the same physical input; public device/output stay intact."""
    monkeypatch.setenv('HOMECAM_BACKEND_URL', 'https://robot.example.com')
    source = 'alsa_input.usb-xfm.mono-fallback'
    monkeypatch.setattr(launch_module, 'shared_xfm_source', lambda env: source)
    context = _context(launch_module, **speech_assets)
    actions = _core_actions(launch_module, context)
    first_capture = next(index for index, item in enumerate(actions)
                         if isinstance(item, GroupAction))
    for action in actions[:first_capture]:
        if isinstance(action, SetEnvironmentVariable):
            action.execute(context)
    assert context.environment['PULSE_SOURCE'] == source
    assert context.environment['MALBUT_SHARED_MICROPHONE'] == source
    media = next(dict(item.launch_arguments)
                 for item in _includes(_readiness_exit(actions, context))
                 if 'backend_url' in dict(item.launch_arguments))
    assert media['audio_source'] == 'pulse'
    assert 'microphone_enabled' not in media
    assert 'audio_sink' not in media
    assert context.launch_configurations['speech_input_device'] == '0'


@pytest.mark.parametrize('speech,cloud,device', [
    ('true', False, '0'), ('false', True, '0'), ('true', True, '2'),
])
def test_other_audio_paths_keep_existing_device_selection(
        launch_module, speech_assets, monkeypatch, speech, cloud, device):
    """Do not add a PulseAudio requirement to standalone STT or explicit overrides."""
    if cloud:
        monkeypatch.setenv('HOMECAM_BACKEND_URL', 'https://robot.example.com')
    monkeypatch.setattr(launch_module, 'shared_xfm_source',
                        lambda env: pytest.fail('unnecessary shared source lookup'))
    speech_assets['speech'] = speech
    context = _context(launch_module, **speech_assets, speech_input_device=device)
    context.environment['MALBUT_SHARED_MICROPHONE'] = 'stale-source'
    context.environment['PULSE_SOURCE'] = 'desktop-source'
    actions = _core_actions(launch_module, context)
    for action in actions:
        if isinstance(action, SetEnvironmentVariable):
            action.execute(context)
    assert context.environment['MALBUT_SHARED_MICROPHONE'] == ''
    assert context.environment['PULSE_SOURCE'] == 'desktop-source'
    assert all('audio_source' not in dict(item.launch_arguments)
               for item in _includes(actions))


def test_robot_defaults_enable_isolated_cuda_speech(launch_module, monkeypatch, tmp_path):
    """The normal robot entrypoint selects the same cache paths as build.sh."""
    monkeypatch.setenv('MALBUT_AGENT_USER_ID', 'http-user')
    monkeypatch.setenv('MALBUT_AGENT_DB', '/http-memory.sqlite3')
    for name in ('MALBUT_SPEECH_RUNTIME', 'MALBUT_STT_MODEL_PATH',
                 'MALBUT_STT_BUILD_DIR', 'MALBUT_STT_LIBRARY_PATH'):
        monkeypatch.delenv(name, raising=False)
    context = LaunchContext()
    for action in launch_module.generate_launch_description().entities:
        if isinstance(action, DeclareLaunchArgument):
            action.execute(context)
    settings = context.launch_configurations
    cache = tmp_path / 'home/.cache/malbut_speech'
    assert settings['speech'] == 'true'
    assert settings['speech_input_device'] == '0'
    assert settings['speech_agent_user_id'] == 'speech-development-user'
    assert settings['speech_agent_conversation_db'] == (
        '~/.local/state/malbut/speech-dialogue.sqlite3')
    assert settings['speech_python_executable'] == str(cache / 'runtime/bin/python')
    assert settings['stt_model_path'] == str(cache / 'models/ggml-small.bin')
    assert settings['stt_library_path'] == str(
        cache / 'whisper-cpp-build/bin/libmalbut_whisper.so')
    assert settings['speech_python_executable'] != settings['python_executable']
    assert settings['speech_python_executable'] != settings['reid_python_executable']


@pytest.mark.parametrize('input_device', [None, '2', '-1'])
def test_speech_starts_after_readiness_and_waits_for_the_manager(
        launch_module, speech_assets, input_device, monkeypatch):
    """Speech forwards the XFM default or explicit override after readiness."""
    monkeypatch.setattr(launch_module, 'nav2_actions', lambda *_args, **_kwargs: [])
    input_options = {} if input_device is None else {'speech_input_device': input_device}
    context = _context(launch_module, **speech_assets, start_hardware='false',
                       **input_options, speech_output_device='3',
                       stt_cpp_threads='4', speech_input_has_aec='true',
                       speech_agent_provider='mock', speech_preflight_timeout_s='55',
                       speech_agent_user_id='trial-p3',
                       speech_agent_conversation_db='/trial records/p3.sqlite3',
                       speech_peer_timeout_s='12', preflight_only='true')
    actions = _core_actions(launch_module, context)
    assert not any('stt_model_path' in dict(item.launch_arguments)
                   for item in _includes(actions))
    wait = next(item for item in actions if isinstance(item, Node)
                and item.node_executable == 'wait_for_robot')
    started = _process_exit(actions, context, wait)
    speech = next(item for item in _includes(started)
                  if 'stt_model_path' in dict(item.launch_arguments))
    assert dict(speech.launch_arguments) == {
        'python_executable': speech_assets['speech_python_executable'],
        'stt_model_path': speech_assets['stt_model_path'],
        'stt_library_path': speech_assets['stt_library_path'],
        'input_device': '0' if input_device is None else input_device,
        'output_device': '3', 'cpp_threads': '4',
        'input_has_aec': 'true', 'agent_provider': 'mock',
        'agent_user_id': 'trial-p3',
        'agent_conversation_db': '/trial records/p3.sqlite3',
        'manager_commands': 'true',
        'navigation_targets': '',
        'control_server': 'manager',
        'preflight_timeout_s': '55', 'peer_timeout_s': '12',
        'preflight_only': 'false', 'use_sim_time': 'false',
    }
    # Do not resolve the venv symlink or replace the parent's perception Python.
    assert context.launch_configurations['python_executable'] != (
        speech_assets['speech_python_executable'])
    assert context.launch_configurations['model_path'].endswith('yolo26n.pt')
    context._set_is_shutdown(True)
    assert _process_exit(actions, context, wait) == []


@pytest.mark.parametrize('name', ['speech_agent_user_id', 'speech_agent_conversation_db'])
@pytest.mark.parametrize('blank', ['', ' \t'])
def test_blank_speech_identity_fails_before_hardware(
        launch_module, speech_assets, monkeypatch, name, blank):
    """Reject an empty participant identity before including any robot child."""
    context = _context(launch_module, **speech_assets, **{name: blank})
    monkeypatch.setattr(launch_module, '_include',
                        lambda *_args, **_kwargs: pytest.fail('child constructed'))
    with pytest.raises(RuntimeError, match=name):
        launch_module._setup(context)


@pytest.mark.parametrize('path', [
    'speech_python_executable', 'stt_model_path', 'stt_library_path',
])
def test_missing_speech_assets_fail_before_hardware(launch_module, speech_assets, path):
    """Never start the robot with a known missing runtime, model or bridge."""
    speech_assets[path] = '/not/prepared'
    context = _context(launch_module, **speech_assets)
    launch_module._include = lambda *_args, **_kwargs: pytest.fail('child constructed')
    with pytest.raises(RuntimeError, match='file not found'):
        _core_actions(launch_module, context)


@pytest.mark.parametrize('check', [
    'speech_control_readiness', 'speech_preflight', 'speech_peer_readiness',
])
def test_parent_allows_successful_speech_checks_but_propagates_failure(launch_module, check):
    """Nested one-shot checks may exit 0; failures must return nonzero to the shell."""
    context = _context(launch_module, start_hardware='false', perception='false')
    actions = _core_actions(launch_module, context)
    process = ExecuteProcess(cmd=['/bin/true'], name=check)
    assert _process_exit(actions, context, process) == []
    with pytest.raises(RuntimeError, match='Bringup child exited'):
        _process_exit(actions, context, process, returncode=2)


@pytest.mark.parametrize('cuda_oom,expected_code,attempts', [(True, 0, 2), (False, 1, 1)])
def test_parent_waits_for_supervised_cuda_oom_retry(
        launch_module, tmp_path, cuda_oom, expected_code, attempts):
    """Hide only an owned startup OOM retry from the real parent launch guard."""
    counter = tmp_path / 'attempts'
    child = tmp_path / 'speech_child.py'
    failure = ('cudaMalloc failed: out of memory' if cuda_oom
               else 'failed to load model: invalid header')
    child.write_text(
        'from pathlib import Path\nimport os, resource, signal\n'
        'resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n'
        f'counter = Path({str(counter)!r})\n'
        'attempt = int(counter.read_text()) + 1 if counter.exists() else 1\n'
        'counter.write_text(str(attempt))\n'
        'if attempt == 1:\n'
        f'    print({failure!r}, flush=True)\n'
        '    os.kill(os.getpid(), signal.SIGABRT)\n')
    process = ExecuteProcess(cmd=[
        sys.executable, '-m', 'malbut_bringup.speech_process',
        '--startup-timeout-s', '20', '--', sys.executable, str(child),
    ], name='speech_preflight', output='screen')
    context = _context(launch_module, start_hardware='false', perception='false')
    event = ProcessExited(action=process, name='speech_preflight', cmd=[],
                          cwd=None, env=None, pid=1, returncode=0)
    guard = next(action for action in _core_actions(launch_module, context)
                 if isinstance(action, RegisterEventHandler)
                 and action.event_handler.matches(event))
    service = LaunchService()
    service.include_launch_description(LaunchDescription([
        guard,
        RegisterEventHandler(OnProcessExit(
            target_action=process,
            on_exit=[EmitEvent(event=Shutdown(reason='supervised process completed'))],
        )),
        process,
    ]))
    assert service.run() == expected_code
    assert int(counter.read_text()) == attempts


@pytest.mark.parametrize('package', ['malbut_stt', 'malbut_tts', 'malbut_agent_server'])
def test_parent_never_leaves_partial_speech_pipeline(launch_module, package):
    """Even a clean persistent-node exit terminates the unified launch as failure."""
    context = _context(launch_module, start_hardware='false', perception='false')
    actions = _core_actions(launch_module, context)
    node = Node(package=package, executable='test')
    with pytest.raises(RuntimeError, match='Bringup child exited'):
        _process_exit(actions, context, node)


def test_managed_successful_bringup_keeps_surviving_nodes_for_manual_recovery(launch_module):
    """Managed launches preserve evidence even before the first READY."""
    context = _context(launch_module, start_hardware='false', perception='false')
    actions = _core_actions(launch_module, context)
    node = Node(package='malbut_stt', executable='test')
    context.extend_globals({'malbut_recovery_owner': True})
    assert _process_exit(actions, context, node)
    context.extend_globals({'malbut_startup_complete': True})
    result = _process_exit(actions, context, node, returncode=-11)
    assert result and all(type(action).__name__ == 'LogInfo' for action in result)


def test_managed_failed_stage_pauses_and_can_continue_after_manual_recovery(launch_module):
    """No later stage starts until the owner explicitly resumes the failed gate."""
    context = _context(launch_module, perception='false')
    context.extend_globals({'malbut_recovery_owner': True})
    initial = launch_module._setup(context)
    gate = _nodes(initial, 'wait_for_robot')[0]
    result = _process_exit(initial, context, gate, returncode=1)
    assert not _nodes(result, 'wait_for_robot')
    assert context.locals.malbut_startup_failed
    assert not getattr(context.locals, 'malbut_startup_complete', False)
    resume, timeout = context.locals.malbut_startup_resume
    next_stage = resume(context)
    assert timeout > 0
    assert not context.locals.malbut_startup_failed
    assert len(_nodes(next_stage, 'wait_for_robot')) == 1
    assert _process_exit(initial, context, gate) == []


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


@pytest.mark.parametrize('enabled', ['auto', 'true'])
def test_fall_monitor_starts_once_after_readiness(launch_module, fall_config, enabled):
    """Keep one early Manager and start one VLM only after readiness."""
    context = _context(launch_module, start_hardware='false',
                       fall_monitor=enabled, fall_config=str(fall_config),
                       rgb_topic='/robot/camera/rgb')
    actions = _core_actions(launch_module, context)
    assert not any(isinstance(item, Node) and item.node_executable == 'malbut-fall-monitor'
                   for item in actions)
    ready = _readiness_exit(actions, context)
    assert sum(isinstance(item, Node) and item.node_executable == 'system_manager'
               for item in actions) == 1
    assert not any(isinstance(item, Node) and item.node_executable == 'system_manager'
                   for item in ready)
    nodes = [item for item in ready if isinstance(item, Node)
             and item.node_executable == 'malbut-fall-monitor']
    assert len(nodes) == 1
    node = nodes[0]
    assert node.node_package == 'malbut_agent_server'
    parameters = evaluate_parameters(context, node._Node__parameters)[0]
    assert parameters['use_sim_time'] is False
    from uuid import UUID
    assert UUID(parameters['runtime_id'])
    assert UUID(parameters['manager_runtime_id'])
    manager = next(item for item in actions if isinstance(item, Node)
                   and item.node_executable == 'system_manager')
    bindings = evaluate_parameters(context, manager._Node__parameters)[0]
    assert bindings['localization_control'] is True
    assert bindings['ready_topic'] == '/malbut/bringup/status'
    assert not any(key.startswith('fall_') for key in bindings)
    coordinators = _nodes(ready, 'fall_coordinator')
    assert len(coordinators) == 1
    bindings = _parameters(context, coordinators[0])
    assert bindings['vlm_runtime_id'] == parameters['runtime_id']
    assert bindings['runtime_id'] == parameters['manager_runtime_id']
    assert UUID(bindings['bridge_runtime_id'])
    arguments = [perform_substitutions(context, arg) for arg in node.cmd[1:]]
    assert arguments[:3] == ['--config', str(fall_config), '--execute']
    remaps = [(perform_substitutions(context, src), perform_substitutions(context, dst))
              for src, dst in node._Node__remappings]
    assert remaps == [('/configured/rgb', '/robot/camera/rgb')]
    assert not (fall_config.parent / 'not-read.key').exists()
    assert not (fall_config.parent / 'journal.sqlite').exists()
    with pytest.raises(RuntimeError, match='Bringup stage failed'):
        _readiness_exit(_core_actions(launch_module, context), context, returncode=1)


def test_startup_binding_is_shared_with_media_and_changes_on_new_launch(
        launch_module, fall_config):
    context = _context(launch_module, start_hardware='false',
                       fall_monitor='true', fall_config=str(fall_config))
    context.environment['HOMECAM_BACKEND_URL'] = 'https://robot.example.com'
    bindings = []
    for _ in range(2):
        actions = _core_actions(launch_module, context)
        ready = _readiness_exit(actions, context)
        media = next(dict(item.launch_arguments) for item in _includes(ready)
                     if 'fall_bridge_runtime_id' in dict(item.launch_arguments))
        params = _parameters(context, _nodes(ready, 'fall_coordinator')[0])
        vlm = next(item for item in ready if isinstance(item, Node)
                   and item.node_executable == 'malbut-fall-monitor')
        vlm_params = evaluate_parameters(context, vlm._Node__parameters)[0]
        poses = _nodes(ready, 'homecam_detector_node')
        assert len(poses) == 1
        pose_params = _parameters(context, poses[0])
        assert pose_params['fall_runtime_id'] == vlm_params['runtime_id']
        assert pose_params['fall_only'] is True
        assert pose_params['pose_keep_aspect'] is True
        assert not _nodes(actions, 'homecam_detector_node')
        assert params['vlm_runtime_id'] == vlm_params['runtime_id']
        assert params['runtime_id'] == vlm_params['manager_runtime_id']
        for peer in ('bridge', 'manager', 'vlm'):
            field = 'fall_' + peer + '_runtime_id'
            parameter = 'runtime_id' if peer == 'manager' else peer + '_runtime_id'
            assert params[parameter] == media[field]
        bindings.append(params)
    assert bindings[0]['bridge_runtime_id'] != bindings[1]['bridge_runtime_id']


def test_fall_launch_uses_unified_navigation_without_legacy_mode_flags(launch_module):
    """No removed launch mode may become an accidental VLM enable switch."""
    context = _context(launch_module, start_hardware='false')
    assert 'mode' not in context.launch_configurations
    assert 'start_navigation' not in context.launch_configurations


def test_fall_monitor_auto_uses_environment_configuration(launch_module, fall_config, monkeypatch):
    """A prepared robot needs no second command or explicit enable flag."""
    monkeypatch.setenv('MALBUT_FALL_CONFIG', str(fall_config))
    context = _context(launch_module, start_hardware='false')
    assert context.launch_configurations['fall_monitor'] == 'auto'
    ready = _readiness_exit(_core_actions(launch_module, context), context)
    assert any(isinstance(item, Node) and item.node_executable == 'malbut-fall-monitor'
               for item in ready)


def test_missing_fall_pose_model_stops_before_launch(launch_module, fall_config):
    """Configured fall monitoring may not silently start without the pose producer."""
    context = _context(launch_module, start_hardware='false', fall_monitor='true',
                       fall_config=str(fall_config), fall_pose_model_path='/missing/pose.onnx')
    with pytest.raises(RuntimeError, match='Fall pose ONNX model'):
        _core_actions(launch_module, context)


def test_fall_pose_does_not_depend_on_general_perception(launch_module, fall_config):
    """Disabling following/object detection does not suppress fall pose."""
    context = _context(launch_module, start_hardware='false', perception='false',
                       fall_monitor='true', fall_config=str(fall_config))
    ready = _readiness_exit(_core_actions(launch_module, context), context)
    assert len(_nodes(ready, 'homecam_detector_node')) == 1


def test_fall_pose_execution_options_reach_only_dedicated_node(launch_module, fall_config):
    context = _context(
        launch_module, start_hardware='false', perception='false',
        fall_monitor='true', fall_config=str(fall_config), fall_pose_execution_provider='cuda',
        fall_pose_intra_op_num_threads='2', fall_pose_allow_spinning='false',
        fall_pose_opencv_num_threads='1')
    ready = _readiness_exit(_core_actions(launch_module, context), context)
    node = _nodes(ready, 'homecam_detector_node')[0]
    params = evaluate_parameters(context, node._Node__parameters)[0]
    assert params['pose_execution_provider'] == 'cuda'
    assert params['pose_intra_op_num_threads'] == 2
    assert params['pose_allow_spinning'] is False
    assert params['pose_opencv_num_threads'] == 1
    assert params['pose_keep_aspect'] is True
    assert params['pose_inference_fps'] == 5.0


@pytest.mark.parametrize('changes', [
    {'fall_pose_execution_provider': 'auto'}, {'fall_pose_intra_op_num_threads': '-1'},
    {'fall_pose_allow_spinning': 'maybe'}, {'fall_pose_opencv_num_threads': '-1'},
])
def test_fall_pose_rejects_invalid_execution_options(launch_module, fall_config, changes):
    context = _context(launch_module, start_hardware='false', fall_monitor='true',
                       fall_config=str(fall_config), **changes)
    with pytest.raises(RuntimeError, match='Invalid fall pose execution options'):
        launch_module._setup(context)


def test_fall_pose_exit_cannot_leave_a_silent_missing_producer(launch_module, fall_config):
    """Even exit 0 of the persistent detector stops the partial Bringup."""
    context = _context(launch_module, start_hardware='false', perception='false',
                       fall_monitor='true', fall_config=str(fall_config))
    actions = _core_actions(launch_module, context)
    pose = _nodes(_readiness_exit(actions, context), 'homecam_detector_node')[0]
    with pytest.raises(RuntimeError, match='Bringup child exited'):
        _process_exit(actions, context, pose)


@pytest.mark.parametrize('enabled', ['auto', 'true'])
def test_fall_monitor_rejects_invalid_configuration(launch_module, fall_config, enabled):
    """A present but unfinished config must fail before any hardware is launched."""
    data = json.loads(fall_config.read_text())
    data['input_fps'] = None
    fall_config.write_text(json.dumps(data))
    context = _context(launch_module, start_hardware='false',
                       fall_monitor=enabled, fall_config=str(fall_config))
    with pytest.raises(RuntimeError, match='Invalid fall_config'):
        _core_actions(launch_module, context)


def test_fall_monitor_false_does_not_read_configuration(launch_module, fall_config):
    """Operators can disable this process even when its config needs repair."""
    fall_config.write_text('not JSON')
    context = _context(launch_module, start_hardware='false',
                       fall_monitor='false', fall_config=str(fall_config))
    ready = _readiness_exit(_core_actions(launch_module, context), context)
    assert not any(isinstance(item, Node) and item.node_executable == 'malbut-fall-monitor'
                   for item in ready)


def test_fall_monitor_true_requires_configuration(launch_module):
    """Explicit enable must not silently skip a missing configuration."""
    context = _context(launch_module, start_hardware='false',
                       fall_monitor='true')
    with pytest.raises(RuntimeError, match='Fall configuration is missing'):
        _core_actions(launch_module, context)


@pytest.mark.parametrize('code', [0, 2])
def test_fall_monitor_exit_stops_bringup(launch_module, fall_config, code):
    """Do not report a healthy launch after its configured fall monitor exits."""
    context = _context(launch_module, start_hardware='false',
                       fall_config=str(fall_config))
    actions = _core_actions(launch_module, context)
    node = next(item for item in _readiness_exit(actions, context)
                if isinstance(item, Node) and item.node_executable == 'malbut-fall-monitor')
    with pytest.raises(RuntimeError, match='Bringup child exited'):
        _process_exit(actions, context, node, returncode=code)
