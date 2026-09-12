"""Launch composition contracts without starting processes or loading models."""

import importlib.util
from pathlib import Path
import shlex
from xml.etree import ElementTree

from launch import LaunchContext
from launch.actions import (
    DeclareLaunchArgument, GroupAction, IncludeLaunchDescription,
    PopLaunchConfigurations, PushLaunchConfigurations, SetLaunchConfiguration,
)
from launch.utilities import perform_substitutions
from launch_ros.actions import Node
from launch_ros.utilities import evaluate_parameters
import yaml


ROOT = Path(__file__).parents[2]


def _load(package, filename):
    source = ROOT / package / 'launch' / filename
    spec = importlib.util.spec_from_file_location(filename.replace('.', '_'), source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Resolve this source-tree contract without installing consumer packages
    # or creating a test dependency cycle (tracking already depends on YOLO).
    module.get_package_share_directory = lambda name: str(ROOT / name)
    return module.generate_launch_description()


def _context(description):
    context = LaunchContext()
    for action in description.entities:
        if isinstance(action, DeclareLaunchArgument):
            action.execute(context)
    return context


def test_detector_launch_uses_selected_python_and_only_one_detector(monkeypatch):
    """An explicit runtime executes the entry script, even with path spaces."""
    monkeypatch.setenv('XDG_CACHE_HOME', '/tmp/test cache')
    monkeypatch.setenv('MALBUT_YOLO_RUNTIME', '/tmp/prepared runtime')
    description = _load('malbut_yolo', 'yolo.launch.py')
    context = _context(description)
    assert context.launch_configurations['python_executable'] == (
        '/tmp/prepared runtime/bin/python'
    )
    assert context.launch_configurations['model_path'] == (
        '/tmp/test cache/malbut_perception/yolo26n.pt'
    )
    nodes = [action for action in description.entities if isinstance(action, Node)]
    assert len(nodes) == 1
    node = nodes[0]
    assert node.node_package == 'malbut_yolo'
    assert node.node_executable == 'yolo_node'
    context.launch_configurations['python_executable'] = '/tmp/test runtime/bin/python'
    prefix = perform_substitutions(context, node.process_description.prefix)
    assert shlex.split(prefix) == ['/tmp/test runtime/bin/python']
    assert context.launch_configurations['device'] == 'cuda:0'
    assert context.launch_configurations['model_path'].endswith('/yolo26n.pt')
    assert not any(isinstance(action, IncludeLaunchDescription)
                   for action in description.entities)


def test_combined_launch_isolates_each_child_config_and_yolo_namespace():
    """Child config filenames must never leak into the RGB-D localizer."""
    description = _load('malbut_tracking', 'person_detection.launch.py')
    context = _context(description)
    original_config = context.launch_configurations['config']
    context.launch_configurations['namespace'] = 'robot'
    groups = [action for action in description.entities if isinstance(action, GroupAction)]
    assert len(groups) == 2
    child_configs = []
    for group in groups:
        actions = group.get_sub_entities()
        assert any(isinstance(action, PushLaunchConfigurations) for action in actions)
        assert any(isinstance(action, PopLaunchConfigurations) for action in actions)
        # Exercise the same configuration push/set/pop actions launch uses.
        for action in actions:
            if isinstance(action, IncludeLaunchDescription):
                options = dict(action.launch_arguments)
                child_configs.append(options['config'])
                for name, value in options.items():
                    SetLaunchConfiguration(name, value).execute(context)
                if options['config'].endswith('/yolo.yaml'):
                    assert context.launch_configurations['namespace'] == 'yolo'
            elif isinstance(action, (PushLaunchConfigurations, PopLaunchConfigurations)):
                action.execute(context)
        assert context.launch_configurations['config'] == original_config
        assert context.launch_configurations['namespace'] == 'robot'
    assert child_configs[0].endswith('/malbut_yolo/config/yolo.yaml')
    assert child_configs[1].endswith('/malbut_reid/config/person_reidentification.yaml')
    localizers = [action for action in description.entities if isinstance(action, Node)]
    assert len(localizers) == 1
    assert localizers[0].node_package == 'malbut_tracking'
    assert localizers[0].node_executable == 'person_localizer'


def test_combined_launch_keeps_yolo_and_reid_interpreters_separate():
    """A ReID runtime must not accidentally select the YOLO interpreter."""
    description = _load('malbut_tracking', 'person_detection.launch.py')
    context = _context(description)
    context.launch_configurations['python_executable'] = '/tmp/yolo/bin/python'
    context.launch_configurations['reid_python_executable'] = '/tmp/reid/bin/python'
    interpreters = []
    for group in description.entities:
        if not isinstance(group, GroupAction):
            continue
        include = next(item for item in group.get_sub_entities()
                       if isinstance(item, IncludeLaunchDescription))
        options = dict(include.launch_arguments)
        interpreters.append(options['python_executable'].perform(context))
    assert interpreters == ['/tmp/yolo/bin/python', '/tmp/reid/bin/python']


def test_detector_declares_upstream_and_ros_dependencies_without_tracking_dependency():
    """The shared detector must be reusable without depending on its consumer."""
    package = ElementTree.parse(ROOT / 'malbut_yolo/package.xml').getroot()
    dependencies = {item.text for item in package.findall('exec_depend')}
    assert {'yolo_ros', 'yolo_msgs', 'malbut_interfaces', 'rclpy', 'launch_ros'} <= dependencies
    assert 'malbut_tracking' not in dependencies
    assert 'malbut_reid' not in dependencies


def test_scoped_perception_does_not_override_later_follower_config():
    """A later FollowPerson launch must load its own tuning, not localizer YAML."""
    perception = _load('malbut_tracking', 'person_detection.launch.py')
    context = LaunchContext()
    parent_group = GroupAction([perception], scoped=True)
    for action in parent_group.get_sub_entities():
        if action is perception:
            for declaration in perception.entities:
                if isinstance(declaration, DeclareLaunchArgument):
                    declaration.execute(context)
            assert context.launch_configurations['config'].endswith('/person_detection.yaml')
        elif isinstance(action, (PushLaunchConfigurations, PopLaunchConfigurations)):
            action.execute(context)
    assert 'config' not in context.launch_configurations
    follower = _load('malbut_tracking', 'person_following.launch.py')
    for declaration in follower.entities:
        if isinstance(declaration, DeclareLaunchArgument):
            declaration.execute(context)
    assert context.launch_configurations['config'].endswith('/person_following.yaml')


def test_lidar_include_does_not_replace_follower_tuning():
    """Evaluate the follower's actual parameter files after its LiDAR include."""
    description = _load('malbut_tracking', 'person_following.launch.py')
    context = _context(description)
    original_config = context.launch_configurations['config']
    group = next(action for action in description.entities if isinstance(action, GroupAction))
    for action in group.get_sub_entities():
        if isinstance(action, IncludeLaunchDescription):
            for name, value in action.launch_arguments:
                SetLaunchConfiguration(name, value).execute(context)
            lidar = _load('malbut_tracking', 'lidar_foreground.launch.py')
            for declaration in lidar.entities:
                if isinstance(declaration, DeclareLaunchArgument):
                    declaration.execute(context)
            assert context.launch_configurations['config'].endswith('/lidar_foreground.yaml')
        elif isinstance(action, (PushLaunchConfigurations, PopLaunchConfigurations)):
            action.execute(context)
    assert context.launch_configurations['config'] == original_config
    follower = next(action for action in description.entities if isinstance(action, Node))
    parameters = evaluate_parameters(context, follower._Node__parameters)
    files = [item for item in parameters if isinstance(item, Path)]
    assert files == [Path(original_config)]
    configured = yaml.safe_load(files[0].read_text())['person_follower']['ros__parameters']
    assert configured['desired_distance_m'] == 1.0
    assert configured['maximum_linear_speed_mps'] == 0.4
    assert configured['observation_loss_debounce_s'] == 0.75
