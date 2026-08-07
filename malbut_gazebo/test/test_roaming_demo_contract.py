"""Launch contracts for the repeatable Small House roaming demonstration."""

import importlib.util
from pathlib import Path

from launch import LaunchContext
from launch.actions import (
    DeclareLaunchArgument,
    GroupAction,
    IncludeLaunchDescription,
)
from launch.utilities import perform_substitutions
import yaml


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _load_launch():
    launch_file = PACKAGE_ROOT / 'launch' / 'roaming_demo.launch.py'
    spec = importlib.util.spec_from_file_location(
        'malbut_roaming_demo_launch',
        launch_file,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.generate_launch_description()


def _source_name(include):
    source = include.launch_description_source
    source.get_launch_description(LaunchContext())
    return Path(source.location).name


def test_small_house_map_metadata_matches_the_world_coordinate_system():
    """The static map must keep its paired image, scale, and map origin."""
    config = yaml.safe_load(
        (PACKAGE_ROOT / 'maps' / 'small_house.yaml').read_text(
            encoding='utf-8'
        )
    )
    assert config == {
        'image': 'small_house.pgm',
        'resolution': 0.05,
        'origin': [-12.5, -12.5, 0.0],
        'negate': 0,
        'occupied_thresh': 0.65,
        'free_thresh': 0.196,
    }
    assert (PACKAGE_ROOT / 'maps' / config['image']).is_file()


def test_demo_composes_world_navigation_rviz_and_roaming_with_one_pose():
    """Inspect actual include actions and their shared spawn/localization pose."""
    description = _load_launch()
    context = LaunchContext()
    defaults = {
        entity.name: perform_substitutions(context, entity.default_value)
        for entity in description.entities
        if isinstance(entity, DeclareLaunchArgument)
    }
    assert defaults == {
        'use_sim_time': 'true',
        'gui': 'true',
        'headless': 'false',
        'rviz': 'true',
        'autostart': 'true',
        'random_seed': '87',
        'x': '-3.665503',
        'y': '-0.4874',
        'yaw': '0.0',
    }

    simulation_group = next(
        entity
        for entity in description.entities
        if isinstance(entity, GroupAction)
    )
    grouped = simulation_group.get_sub_entities()
    assert type(grouped[0]).__name__ == 'PushLaunchConfigurations'
    assert type(grouped[-1]).__name__ == 'PopLaunchConfigurations'
    simulation = next(
        entity
        for entity in grouped
        if isinstance(entity, IncludeLaunchDescription)
    )
    assert _source_name(simulation) == 'worlds.launch.py'
    simulation_arguments = dict(simulation.launch_arguments)
    assert simulation_arguments['world_name'] == 'small_house'
    assert simulation_arguments['rviz'] == 'false'
    assert simulation_arguments['spawn_robot'] == 'true'
    assert simulation_arguments['bridge'] == 'true'
    assert simulation_arguments['lidar_enabled'] == 'true'

    includes = [
        entity
        for entity in description.entities
        if isinstance(entity, IncludeLaunchDescription)
    ]
    by_source = {_source_name(include): include for include in includes}
    assert set(by_source) == {'navigation.launch.py', 'roaming.launch.py'}
    navigation = dict(by_source['navigation.launch.py'].launch_arguments)
    assert Path(navigation['map']).name == 'small_house.yaml'
    assert navigation['restore_localization'] == 'false'
    assert navigation['set_initial_pose'] == 'true'
    assert navigation['localization_source'] == 'static'
    roaming = dict(by_source['roaming.launch.py'].launch_arguments)
    assert set(roaming) == {'use_sim_time', 'autostart', 'random_seed'}
