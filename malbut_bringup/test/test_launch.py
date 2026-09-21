"""Check composition without launching hardware, inference, or navigation."""

import importlib.util
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
                              ('malbut_reid', 'MALBUT_REID_RUNTIME')):
        runtime = cache / name / 'runtime'
        monkeypatch.setenv(environment, str(runtime))
        executable = runtime / 'bin/python'
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text('#!/bin/sh\nexit 0\n')
        executable.chmod(0o755)
    for name in ('yolo26n.pt', 'osnet_ain_x1_0_msmt17.onnx'):
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


def test_one_bringup_starts_everything_and_maps_without_a_saved_map(launch_module):
    """No modes: hardware, Nav2, applications and the manager always start."""
    context = _context(launch_module)
    actions = launch_module._setup(context)
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


def test_nav2_is_composed_with_collision_monitor_and_zone_filter(launch_module):
    """Navigation publishes /cmd_vel; manual driving passes the Collision Monitor."""
    context = _context(launch_module, scan_topic='/laser_raw')
    actions = launch_module._setup(context)
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
    order = list(navigation['node_names'])
    assert order[:2] == ['zone_filter_mask_server', 'zone_filter_info_server']
    # Spinning to find the pose must not wait for the map-frame global costmap.
    for name in ('behavior_server', 'teleop_behavior_server', 'velocity_smoother',
                 'collision_monitor'):
        assert order.index(name) < order.index('planner_server'), name
    assert list(localization['node_names']) == ['map_server', 'amcl']
    assert _nodes(actions, 'zone_filter')[0].node_package == 'malbut_bringup'


@pytest.mark.parametrize('options', [{'relocalization': 'false'}, {'restore_pose': 'false'}])
def test_pose_finding_can_be_left_to_the_operator(launch_module, options):
    """Without it the manager loads maps and the operator sets the pose."""
    context = _context(launch_module, **options)
    actions = launch_module._setup(context)
    manager = _parameters(context, _nodes(actions, 'system_manager')[0])
    assert manager['relocalize_action'] == ''
    assert bool(_nodes(actions, 'relocalization')) == ('relocalization' not in options)


def test_reused_hardware_is_not_launched_again(launch_module):
    """Externally started drivers keep their own joystick; no duplicates start."""
    context = _context(launch_module, start_hardware='false', perception='false')
    actions = launch_module._setup(context)
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
    actions = launch_module._setup(context)
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
    options = [dict(item.launch_arguments)
               for item in _includes(launch_module._setup(context))]
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
        launch_module._setup(context)


def test_selected_map_starts_saved_map_localization(launch_module, tmp_path):
    """A given map picks the first localization; a missing one fails early."""
    map_path = tmp_path / 'real house.yaml'
    map_path.write_text('image: real_house.pgm\n')
    context = _context(launch_module, map=str(map_path))
    actions = launch_module._setup(context)
    manager = _parameters(context, _nodes(actions, 'system_manager')[0])
    assert manager['initial_map'] == str(map_path)
    with pytest.raises(RuntimeError, match='saved map YAML'):
        launch_module._setup(_context(launch_module, map=str(tmp_path / 'missing.yaml')))


@pytest.mark.parametrize('returncode', [0, 1])
def test_manager_starts_first_and_missions_wait_for_readiness(launch_module, returncode):
    """Localization must precede Nav2 activation; readiness only opens missions."""
    context = _context(launch_module, start_hardware='false')
    actions = launch_module._setup(context)
    assert len(_nodes(actions, 'system_manager')) == 1
    if returncode:
        with pytest.raises(RuntimeError, match='Robot readiness check failed'):
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


def test_robot_defaults_enable_isolated_cuda_speech(launch_module, monkeypatch, tmp_path):
    """The normal robot entrypoint selects the same cache paths as build.sh."""
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
    assert settings['speech_python_executable'] == str(cache / 'runtime/bin/python')
    assert settings['stt_model_path'] == str(cache / 'models/ggml-small.bin')
    assert settings['stt_library_path'] == str(
        cache / 'whisper-cpp-build/bin/libmalbut_whisper.so')
    assert settings['speech_python_executable'] != settings['python_executable']
    assert settings['speech_python_executable'] != settings['reid_python_executable']


@pytest.mark.parametrize('input_device', [None, '2', '-1'])
def test_speech_starts_after_readiness_and_waits_for_the_manager(
        launch_module, speech_assets, input_device):
    """Speech forwards the XFM default or explicit override after readiness."""
    input_options = {} if input_device is None else {'speech_input_device': input_device}
    context = _context(launch_module, **speech_assets, start_hardware='false',
                       **input_options, speech_output_device='3',
                       stt_cpp_threads='4', speech_input_has_aec='true',
                       speech_agent_provider='mock', speech_preflight_timeout_s='55',
                       speech_peer_timeout_s='12', preflight_only='true')
    actions = launch_module._setup(context)
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


@pytest.mark.parametrize('path', [
    'speech_python_executable', 'stt_model_path', 'stt_library_path',
])
def test_missing_speech_assets_fail_before_hardware(launch_module, speech_assets, path):
    """Never start the robot with a known missing runtime, model or bridge."""
    speech_assets[path] = '/not/prepared'
    context = _context(launch_module, **speech_assets)
    launch_module._include = lambda *_args, **_kwargs: pytest.fail('child constructed')
    with pytest.raises(RuntimeError, match='file not found'):
        launch_module._setup(context)


@pytest.mark.parametrize('check', [
    'speech_control_readiness', 'speech_preflight', 'speech_peer_readiness',
])
def test_parent_allows_successful_speech_checks_but_propagates_failure(launch_module, check):
    """Nested one-shot checks may exit 0; failures must return nonzero to the shell."""
    context = _context(launch_module, start_hardware='false', perception='false')
    actions = launch_module._setup(context)
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
    guard = next(action for action in launch_module._setup(context)
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
    actions = launch_module._setup(context)
    node = Node(package=package, executable='test')
    with pytest.raises(RuntimeError, match='Bringup child exited'):
        _process_exit(actions, context, node)
