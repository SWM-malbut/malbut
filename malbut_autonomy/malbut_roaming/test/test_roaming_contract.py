"""Package, configuration, and launch contracts for autonomous roaming."""

import importlib.util
from pathlib import Path
from xml.etree import ElementTree

from launch import LaunchContext
from launch.actions import DeclareLaunchArgument
from launch.utilities import perform_substitutions
from launch_ros.actions import Node
import yaml

from malbut_roaming.policy import PolicyConfig


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = PACKAGE_ROOT / 'config' / 'roaming.yaml'


def _parameters():
    return yaml.safe_load(CONFIG_FILE.read_text(encoding='utf-8'))[
        'roaming_manager'
    ]['ros__parameters']


def _load_launch():
    launch_file = PACKAGE_ROOT / 'launch' / 'roaming.launch.py'
    spec = importlib.util.spec_from_file_location(
        'malbut_roaming_launch',
        launch_file,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.generate_launch_description()


def test_package_depends_on_nav2_interfaces_but_not_the_simulator():
    """Reusable roaming may use Nav2 APIs without depending on Gazebo."""
    root = ElementTree.parse(PACKAGE_ROOT / 'package.xml').getroot()
    dependencies = {
        element.text
        for element in root
        if element.tag in {'depend', 'exec_depend'}
    }
    assert {
        'action_msgs',
        'geometry_msgs',
        'lifecycle_msgs',
        'nav2_msgs',
        'nav_msgs',
        'rclpy',
        'tf2_ros_py',
    } <= dependencies
    assert 'malbut_gazebo' not in dependencies
    assert 'ros_gz_sim' not in dependencies


def test_deployment_configuration_builds_a_valid_policy():
    """All policy settings in the shipped YAML must satisfy runtime bounds."""
    config = _parameters()
    policy = PolicyConfig(
        minimum_goal_distance=config['minimum_goal_distance'],
        maximum_goal_distance=config['maximum_goal_distance'],
        preferred_goal_distance=config['preferred_goal_distance'],
        distance_scale=config['distance_scale'],
        open_clearance=config['open_clearance'],
        peripheral_clearance=config['peripheral_clearance'],
        peripheral_probability=config['peripheral_probability'],
        revisit_horizon_seconds=config['revisit_horizon_seconds'],
        recent_goal_radius=config['recent_goal_radius'],
        recent_memory_size=config['recent_memory_size'],
        failure_cooldown_seconds=config['failure_cooldown_seconds'],
        idleness_weight=config['idleness_weight'],
        distance_weight=config['distance_weight'],
        clearance_weight=config['clearance_weight'],
        novelty_weight=config['novelty_weight'],
        top_k=config['selection_top_k'],
        temperature=config['selection_temperature'],
    )
    policy.validate()
    assert config['autostart'] is False
    assert config['minimum_clearance'] <= config['peripheral_clearance']
    assert config['peripheral_clearance'] < config['open_clearance']
    assert config['map_frame'] == 'map'
    assert config['base_frame'] == 'base_footprint'
    assert config['filter_mask_topic'] == ''
    assert config['filter_restricted_threshold'] == 100


def test_launch_exposes_overrides_and_starts_only_the_reusable_node():
    """Inspect the generated launch description instead of source strings."""
    description = _load_launch()
    context = LaunchContext()
    defaults = {
        entity.name: perform_substitutions(context, entity.default_value)
        for entity in description.entities
        if isinstance(entity, DeclareLaunchArgument)
    }
    assert Path(defaults.pop('params_file')).name == CONFIG_FILE.name
    assert defaults == {
        'use_sim_time': 'true',
        'autostart': 'false',
        'random_seed': '-1',
    }
    nodes = [
        entity
        for entity in description.entities
        if isinstance(entity, Node)
    ]
    assert len(nodes) == 1
    assert nodes[0].node_package == 'malbut_roaming'
    assert nodes[0].node_executable == 'roaming_manager'
