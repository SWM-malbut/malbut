"""Configuration and launch-composition tests for online SLAM."""

import importlib.util
from pathlib import Path

from launch import LaunchContext
from launch.actions import (
    DeclareLaunchArgument,
    GroupAction,
    IncludeLaunchDescription,
)
from launch.utilities import perform_substitutions
from launch_ros.actions import Node
import pytest
import yaml

from malbut_description.variant_config import load_variant_arguments


GAZEBO_ROOT = Path(__file__).resolve().parents[1]
DESCRIPTION_ROOT = GAZEBO_ROOT.parent / 'malbut_description'
PROFILE = DESCRIPTION_ROOT / 'config' / 'rosorin_ultimate_mecanum.yaml'


def _load_launch(filename, module_name):
    launch_file = GAZEBO_ROOT / 'launch' / filename
    spec = importlib.util.spec_from_file_location(module_name, launch_file)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.generate_launch_description()


def _source_path(include):
    source = include.launch_description_source
    source.get_launch_description(LaunchContext())
    return Path(source.location)


def _declared_defaults(description):
    context = LaunchContext()
    return {
        entity.name: perform_substitutions(context, entity.default_value)
        for entity in description.entities
        if isinstance(entity, DeclareLaunchArgument)
    }


def test_slam_parameters_match_the_robot_frames_and_lidar():
    """SLAM Toolbox must consume the generated robot's public scan contract."""
    parameters = yaml.safe_load(
        (GAZEBO_ROOT / 'config' / 'slam.yaml').read_text(encoding='utf-8')
    )['/**']['ros__parameters']
    arguments = load_variant_arguments(PROFILE)

    assert parameters['mode'] == 'mapping'
    assert parameters['map_frame'] == 'map'
    assert parameters['odom_frame'] == 'odom'
    assert parameters['base_frame'] == 'base_footprint'
    assert parameters['scan_topic'] == 'scan'
    assert parameters['max_laser_range'] == pytest.approx(
        arguments['lidar_max_range']
    )
    assert parameters['scan_buffer_maximum_scan_distance'] == pytest.approx(
        arguments['lidar_max_range']
    )
    assert parameters['resolution'] > 0.0
    assert parameters['transform_publish_period'] > 0.0
    assert parameters['use_scan_matching'] is True
    assert parameters['do_loop_closing'] is True


def test_slam_rviz_view_uses_map_scan_and_mapping_frame():
    """The checked-in RViz view must show the actual mapping outputs."""
    config = yaml.safe_load(
        (GAZEBO_ROOT / 'rviz' / 'slam.rviz').read_text(encoding='utf-8')
    )
    manager = config['Visualization Manager']

    def display(class_name):
        matches = [
            item
            for item in manager['Displays']
            if item['Class'] == class_name
        ]
        assert len(matches) == 1
        return matches[0]

    assert manager['Global Options']['Fixed Frame'] == 'map'
    assert display('rviz_default_plugins/Map')['Topic']['Value'] == '/map'
    scan = display('rviz_default_plugins/LaserScan')['Topic']
    assert scan['Value'] == '/scan'
    assert scan['Reliability Policy'] == 'Best Effort'


def test_slam_launch_composes_simulator_mapper_rviz_and_handoff_node():
    """Inspect launch actions instead of searching implementation strings."""
    description = _load_launch('slam.launch.py', 'malbut_slam_launch')
    defaults = _declared_defaults(description)
    assert defaults['world_name'] == 'small_house'
    assert defaults['variant_config'] == 'rosorin_ultimate_mecanum.yaml'
    assert defaults['rviz'] == 'true'
    assert Path(defaults['slam_params_file']).name == 'slam.yaml'
    assert Path(defaults['rviz_config']).name == 'slam.rviz'

    simulation_group = next(
        entity
        for entity in description.entities
        if isinstance(entity, GroupAction)
    )
    group_entities = simulation_group.get_sub_entities()
    assert type(group_entities[0]).__name__ == 'PushLaunchConfigurations'
    assert type(group_entities[-1]).__name__ == 'PopLaunchConfigurations'
    simulation = next(
        entity
        for entity in group_entities
        if isinstance(entity, IncludeLaunchDescription)
    )
    assert _source_path(simulation).name == 'worlds.launch.py'
    simulation_arguments = dict(simulation.launch_arguments)
    assert simulation_arguments['rviz'] == 'false'
    assert simulation_arguments['lidar_enabled'] == 'true'
    assert simulation_arguments['spawn_robot'] == 'true'
    assert simulation_arguments['bridge'] == 'true'

    includes = [
        entity
        for entity in description.entities
        if isinstance(entity, IncludeLaunchDescription)
    ]
    mapper = next(
        include
        for include in includes
        if _source_path(include).name == 'online_async_launch.py'
    )
    assert set(dict(mapper.launch_arguments)) == {
        'use_sim_time',
        'slam_params_file',
    }

    nodes = [
        entity
        for entity in description.entities
        if isinstance(entity, Node)
    ]
    assert {
        (node.node_package, node.node_executable) for node in nodes
    } == {
        ('malbut_gazebo', 'record_localization_state'),
        ('rviz2', 'rviz2'),
    }
    assert 'localization_state' in defaults
