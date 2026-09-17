"""Check composition without launching hardware, inference, or navigation."""

import importlib.util
from pathlib import Path
from xml.etree import ElementTree

from launch import LaunchContext
from launch.actions import (
    DeclareLaunchArgument, ExecuteProcess, GroupAction, IncludeLaunchDescription,
    RegisterEventHandler, SetEnvironmentVariable,
)
from launch.events.process import ProcessExited
from launch_ros.actions import Node, SetParameter
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
    for name in ('slam/launch/include/robot.launch.py',
                 'home/ros2_ws/src/navigation/launch/include/bringup.launch.py'):
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


def test_default_without_map_only_starts_hardware_and_perception(launch_module):
    """A fresh robot must not silently use the simulation map or start moving."""
    context = _context(launch_module)
    actions = launch_module._setup(context)
    includes = _includes(actions)
    assert len(includes) == 2
    options = [dict(item.launch_arguments) for item in includes]
    assert options[0]['sim'] == 'false'
    assert options[0]['robot_name'] == '/'
    assert options[0]['master_name'] == '/'
    assert options[0]['point_cloud_enable'] == 'false'
    assert options[1]['reid_backend'] == 'osnet'
    assert Path(options[1]['python_executable']).is_file()
    assert Path(options[1]['model_path']).is_file()
    assert not any(isinstance(action, Node)
                   and action.node_package == 'malbut_system_manager'
                   for action in actions)


def test_hardware_only_does_not_require_vendor_or_gpu_install(launch_module):
    """Externally started drivers can be checked without restarting them."""
    launch_module.get_package_share_directory = lambda _: pytest.fail('unexpected lookup')
    context = _context(launch_module, start_hardware='false', perception='false')
    actions = launch_module._setup(context)
    assert _includes(actions) == []
    assert [item.node_executable for item in actions if isinstance(item, Node)] == [
        'wait_for_robot']


def test_mapping_prepares_camera_before_exposing_idle_autoslam(launch_module):
    """Show the camera first; reuse the ready drivers when a Goal starts mapping."""
    context = _context(launch_module, mode='mapping', web_panel='true',
                       scan_topic='/laser_raw',
                       map_directory='/configured/maps', static_map_topic='/mapping/map',
                       robot_frame='robot/base', rgb_topic='/camera/color',
                       python_executable='/not/prepared')
    actions = launch_module._setup(context)
    includes = _includes(actions)
    assert len(includes) == 1
    assert dict(includes[0].launch_arguments)['sim'] == 'false'
    ready = _readiness_exit(actions, context)
    options = dict(_includes(ready)[0].launch_arguments)
    assert options['auto_start'] == 'true'
    assert options['scan_topic'] == '/laser_raw'
    assert options['use_sim_time'] == 'false'
    assert options['map_directory'] == '/configured/maps'
    assert options['map_topic'] == '/mapping/map'
    assert options['base_frame'] == 'robot/base'
    assert [item.node_executable for item in actions if isinstance(item, Node)] == [
        'robot_web_panel', 'wait_for_robot']
    panel = next(item for item in actions if isinstance(item, Node)
                 and item.node_executable == 'robot_web_panel')
    assert evaluate_parameters(context, panel._Node__parameters)[0] == {
        'use_sim_time': False, 'manage_bringup': False,
        'map_directory': '/configured/maps', 'map_topic': '/global_costmap/costmap',
        'robot_frame': 'robot/base', 'rgb_topic': '/camera/color',
    }
    wait = next(item for item in actions if isinstance(item, Node)
                and item.node_executable == 'wait_for_robot')
    settings = evaluate_parameters(context, wait._Node__parameters)[0]
    assert settings['perception'] is False and settings['navigation'] is False
    with pytest.raises(RuntimeError, match='Robot readiness check failed'):
        _readiness_exit(actions, context, returncode=1)


@pytest.mark.parametrize('mode', ['mapping', 'navigation'])
def test_cloud_bringup_includes_one_media_sender_on_real_topics(launch_module, mode):
    """The bridge stays separate; media follows either Bringup mode's lifetime."""
    context = _context(launch_module, mode=mode, start_hardware='false',
                       start_navigation='false', rgb_topic='/camera/color',
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
    assert not any('auto_start' in item for item in options)


def test_missing_perception_files_fail_before_constructing_hardware(launch_module):
    """Report missing inference setup before any child launch can be returned."""
    context = _context(launch_module, python_executable='/not/prepared')
    launch_module._include = lambda *_args, **_kwargs: pytest.fail('child constructed')
    with pytest.raises(RuntimeError, match='Perception files are not ready'):
        launch_module._setup(context)


def test_navigation_requires_explicit_real_map(launch_module):
    """Fail before returning any launch actions if map selection is missing."""
    with pytest.raises(RuntimeError, match='real saved map YAML'):
        launch_module._setup(_context(launch_module, mode='navigation'))


def test_navigation_keeps_each_child_scoped_and_wall_timed(launch_module, tmp_path):
    """Use vendor configuration and all three Malbut application launches."""
    map_path = tmp_path / 'real house.yaml'
    map_path.write_text('image: real_house.pgm\n')
    context = _context(launch_module, mode='navigation', map=str(map_path), web_panel='true')
    actions = launch_module._setup(context)
    includes = _includes(actions)
    assert len(includes) == 5
    options = [dict(item.launch_arguments) for item in includes]
    assert all(item['use_sim_time'] == 'false' for item in options)
    nav = next(item for item in options if 'map' in item)
    assert nav['map'] == str(map_path)
    assert nav['params_file'].endswith('malbut_bringup/config/nav2_params.yaml')
    assert nav['use_namespace'] == 'false'
    nav_source = includes[1].launch_description_source
    nav_source.get_launch_description(context)
    assert nav_source.location.endswith(
        'home/ros2_ws/src/navigation/launch/include/bringup.launch.py')
    assert sum('model_path' in item for item in options) == 1
    assert sum('scan_topic' in item for item in options) == 1
    follower = next(item for item in options if 'scan_topic' in item)
    assert follower['scan_topic'] == '/scan_raw'
    assert follower['lidar_config'].endswith('malbut_tracking/config/lidar_foreground.yaml')
    assert Path(follower['lidar_config']).is_file()
    panel = next(item for item in actions if isinstance(item, Node)
                 and item.node_executable == 'robot_web_panel')
    assert evaluate_parameters(context, panel._Node__parameters)[0]['manage_bringup'] is False
    for group in [item for item in actions if isinstance(item, GroupAction)]:
        # A global -p creates /** before the named config. In rclcpp this can
        # make that config's /scan beat the later inline /scan_raw.
        assert not any(isinstance(child, SetParameter) for child in group.get_sub_entities())
    assert not any(isinstance(action, Node)
                   and action.node_package == 'malbut_system_manager'
                   for action in actions)

    assert any(isinstance(action, Node) and action.node_executable == 'pose_memory'
               for action in actions)
    disabled = launch_module._setup(_context(
        launch_module, mode='navigation', map=str(map_path), pose_memory='false'))
    assert not any(isinstance(action, Node) and action.node_executable == 'pose_memory'
                   for action in disabled)


def test_external_navigation_does_not_load_another_map_or_nav2(launch_module):
    """An explicitly external navigation stack is reused as-is."""
    context = _context(launch_module, mode='navigation', start_hardware='false',
                       start_navigation='false')
    actions = launch_module._setup(context)
    assert len(_includes(actions)) == 3


@pytest.mark.parametrize('returncode,starts_manager', [(0, True), (1, False)])
def test_manager_only_starts_after_successful_readiness(
        launch_module, returncode, starts_manager):
    """Exercise the real launch process-exit handlers without spawning nodes."""
    context = _context(launch_module, mode='navigation', start_hardware='false',
                       start_navigation='false')
    actions = launch_module._setup(context)
    result = []
    if starts_manager:
        result = _readiness_exit(actions, context, returncode=returncode)
    else:
        with pytest.raises(RuntimeError, match='Robot readiness check failed'):
            _readiness_exit(actions, context, returncode=returncode)
    managers = [item for item in result if isinstance(item, Node)
                and item.node_package == 'malbut_system_manager']
    assert len(managers) == int(starts_manager)


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
    assert settings['speech_python_executable'] == str(cache / 'runtime/bin/python')
    assert settings['stt_model_path'] == str(cache / 'models/ggml-small.bin')
    assert settings['stt_library_path'] == str(
        cache / 'whisper-cpp-build/bin/libmalbut_whisper.so')
    assert settings['speech_python_executable'] != settings['python_executable']
    assert settings['speech_python_executable'] != settings['reid_python_executable']


@pytest.mark.parametrize('mode', ['sensors', 'navigation', 'mapping'])
def test_all_modes_include_speech_after_their_readiness_gate(
        launch_module, speech_assets, mode):
    """Every mode must finish robot readiness before starting speech."""
    context = _context(launch_module, **speech_assets, mode=mode, start_navigation='false',
                       speech_input_device='2', speech_output_device='3',
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
        'input_device': '2', 'output_device': '3', 'cpp_threads': '4',
        'input_has_aec': 'true', 'agent_provider': 'mock',
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


@pytest.mark.parametrize('check', ['speech_preflight', 'speech_peer_readiness'])
def test_parent_allows_successful_speech_checks_but_propagates_failure(launch_module, check):
    """Nested one-shot checks may exit 0; failures must return nonzero to the shell."""
    context = _context(launch_module, start_hardware='false', perception='false')
    actions = launch_module._setup(context)
    process = ExecuteProcess(cmd=['/bin/true'], name=check)
    assert _process_exit(actions, context, process) == []
    with pytest.raises(RuntimeError, match='Bringup child exited'):
        _process_exit(actions, context, process, returncode=2)


@pytest.mark.parametrize('package', ['malbut_stt', 'malbut_tts', 'malbut_agent_server'])
def test_parent_never_leaves_partial_speech_pipeline(launch_module, package):
    """Even a clean persistent-node exit terminates the unified launch as failure."""
    context = _context(launch_module, start_hardware='false', perception='false')
    actions = launch_module._setup(context)
    node = Node(package=package, executable='test')
    with pytest.raises(RuntimeError, match='Bringup child exited'):
        _process_exit(actions, context, node)
