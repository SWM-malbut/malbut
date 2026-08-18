"""Launch contracts for the sensor-driven person-following demonstration."""

import importlib.util
from pathlib import Path

from launch import LaunchContext
from launch.actions import (
    DeclareLaunchArgument,
    GroupAction,
    IncludeLaunchDescription,
)
from launch.utilities import perform_substitutions
from launch_ros.actions import Node, SetRemap


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _load_launch():
    launch_file = PACKAGE_ROOT / 'launch' / 'target_tracking_demo.launch.py'
    spec = importlib.util.spec_from_file_location(
        'malbut_target_tracking_demo_launch',
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


def test_demo_composes_sensor_perception_nav2_and_tracking():
    """One launch must assemble the real RGB-D-to-Nav2 tracking pipeline."""
    description = _load_launch()
    context = LaunchContext()
    defaults = {
        entity.name: perform_substitutions(context, entity.default_value)
        for entity in description.entities
        if isinstance(entity, DeclareLaunchArgument)
    }
    assert defaults['world_name'] == 'small_house'
    assert defaults['rviz'] == 'true'
    assert defaults['image_view'] == 'true'
    assert defaults['actor_spawn_delay'] == '15.0'
    assert defaults['debug_image_transport'] == 'raw'

    image_view_nodes = [
        entity
        for entity in description.entities
        if isinstance(entity, Node)
        and entity.node_package == 'rqt_image_view'
    ]
    assert len(image_view_nodes) == 1
    assert image_view_nodes[0].node_executable == 'rqt_image_view'
    assert image_view_nodes[0]._Node__arguments == [
        '/perception/person/debug_image'
    ]

    simulation_group = next(
        entity
        for entity in description.entities
        if isinstance(entity, GroupAction)
    )
    humanoid = next(
        entity
        for entity in simulation_group.get_sub_entities()
        if isinstance(entity, IncludeLaunchDescription)
    )
    assert _source_name(humanoid) == 'humanoid_demo.launch.py'
    humanoid_arguments = dict(humanoid.launch_arguments)
    assert humanoid_arguments['perception'] == 'true'
    assert humanoid_arguments['rviz'] == 'false'
    assert 'actor_name' in humanoid_arguments
    assert 'actor_z' in humanoid_arguments
    assert 'spawn_timeout' in humanoid_arguments
    assert (
        humanoid_arguments['projection_frame']
        == 'camera_depth_optical_frame'
    )
    assert 'debug_image_transport' in humanoid_arguments
    remap = next(
        entity
        for entity in simulation_group.get_sub_entities()
        if isinstance(entity, SetRemap)
    )
    assert remap is not None

    includes = [
        entity
        for entity in description.entities
        if isinstance(entity, IncludeLaunchDescription)
    ]
    by_source = {_source_name(include): include for include in includes}
    assert set(by_source) == {
        'navigation.launch.py',
        'person_following.launch.py',
    }
    navigation = dict(by_source['navigation.launch.py'].launch_arguments)
    assert Path(navigation['map']).name == 'small_house.yaml'
    assert navigation['localization_source'] == 'static'
    assert navigation['set_initial_pose'] == 'true'
    tracking = dict(by_source['person_following.launch.py'].launch_arguments)
    assert set(tracking) == {'use_sim_time'}

    collision_nodes = [
        entity
        for entity in description.entities
        if isinstance(entity, Node)
        and entity.node_package == 'nav2_collision_monitor'
    ]
    assert len(collision_nodes) == 1
    launch_source = (
        PACKAGE_ROOT / 'launch' / 'target_tracking_demo.launch.py'
    ).read_text(encoding='utf-8')
    assert "'cmd_vel_in_topic': '/cmd_vel'" in launch_source
    assert "'cmd_vel_out_topic': '/cmd_vel_tracking_output'" in (
        launch_source
    )
    assert '/cmd_vel_tracking_raw' not in launch_source


def test_demo_uses_optical_sensor_coordinates_without_ground_truth():
    """Projected detections must use the camera optical TF, not model poses."""
    target_demo = (
        PACKAGE_ROOT / 'launch' / 'target_tracking_demo.launch.py'
    ).read_text(encoding='utf-8')
    humanoid_demo = (
        PACKAGE_ROOT / 'launch' / 'humanoid_demo.launch.py'
    ).read_text(encoding='utf-8')
    tracking_source = '\n'.join(
        path.read_text(encoding='utf-8')
        for path in (
            PACKAGE_ROOT.parent
            / 'malbut_autonomy'
            / 'malbut_tracking'
            / 'malbut_tracking'
        ).glob('*.py')
    )
    combined = target_demo + tracking_source
    assert 'camera_depth_optical_frame' in humanoid_demo
    assert 'Detection3DArray' in tracking_source
    assert 'model_pose' not in combined
    assert '/world/' not in combined
