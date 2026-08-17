"""Package, map, launch, and interface contracts for SWM25-94."""

import importlib.util
import json
from pathlib import Path
from xml.etree import ElementTree

import cv2
from launch import LaunchContext
from launch.actions import (
    DeclareLaunchArgument,
    GroupAction,
    IncludeLaunchDescription,
)
from launch.utilities import perform_substitutions
from launch_ros.actions import Node, SetRemap
import yaml

from malbut_gazebo.user_map_builder import load_slam_map
from malbut_gazebo.zone_filter_mask import load_zones
from malbut_scenarios.scenario_config import load_room_routes


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPOSITORY_ROOT / 'malbut_scenarios'
GAZEBO_ROOT = REPOSITORY_ROOT / 'malbut_gazebo'


def _load_launch():
    launch_path = PACKAGE_ROOT / 'launch' / 'autonomous_driving.launch.py'
    spec = importlib.util.spec_from_file_location(
        'malbut_autonomous_driving_launch', launch_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.generate_launch_description()


def _source_name(include):
    source = include.launch_description_source
    source.get_launch_description(LaunchContext())
    return Path(source.location).name


def test_package_declares_only_public_ros_dependencies():
    root = ElementTree.parse(PACKAGE_ROOT / 'package.xml').getroot()
    dependencies = {
        element.text
        for element in root
        if element.tag in {'depend', 'exec_depend'}
    }

    assert {
        'malbut_gazebo',
        'malbut_interfaces',
        'malbut_perception',
        'malbut_roaming',
        'malbut_tracking',
        'nav2_msgs',
        'rclpy',
    } <= dependencies
    assert 'ros_gz_interfaces' not in dependencies


def test_one_launch_composes_existing_features_and_velocity_arbitration():
    description = _load_launch()
    context = LaunchContext()
    defaults = {
        entity.name: perform_substitutions(context, entity.default_value)
        for entity in description.entities
        if isinstance(entity, DeclareLaunchArgument)
    }
    assert defaults['actor_spawn_delay'] == '180.0'
    assert defaults['cloud_sync'] == 'false'
    assert defaults['patrol_autostart'] == 'false'
    assert defaults['rviz'] == 'true'

    top_level_includes = [
        entity
        for entity in description.entities
        if isinstance(entity, IncludeLaunchDescription)
    ]
    include_names = {_source_name(item) for item in top_level_includes}
    assert include_names == {
        'humanoid_demo.launch.py',
        'roaming.launch.py',
        'person_following.launch.py',
    }

    navigation_group = next(
        entity
        for entity in description.entities
        if isinstance(entity, GroupAction)
    )
    grouped = navigation_group.get_sub_entities()
    remap = next(
        action
        for action in grouped
        if isinstance(action, SetRemap)
    )
    assert perform_substitutions(
        context, remap._SetRemap__src
    ) == '/cmd_vel'
    assert perform_substitutions(
        context, remap._SetRemap__dst
    ) == '/scenario/nav_cmd_vel'
    navigation = next(
        action
        for action in grouped
        if isinstance(action, IncludeLaunchDescription)
    )
    arguments = dict(navigation.launch_arguments)
    assert arguments['robot_web_navigation_action'] == (
        '/scenario/navigate_to_pose'
    )
    assert str(arguments['zone_mask']).endswith('/maps/zone-filter.yaml')
    assert str(arguments['user_map']).endswith(
        '/maps/small_house_user_map.geojson'
    )

    nodes = [
        entity for entity in description.entities if isinstance(entity, Node)
    ]
    executables = {entity.node_executable for entity in nodes}
    assert {
        'autonomous_driving_manager',
        'cloud_robot_sync',
        'collision_monitor',
        'lifecycle_manager',
        'rqt_image_view',
    } <= executables


def test_demo_user_map_and_existing_zone_pipeline_share_one_map_identity():
    slam_map = load_slam_map(
        GAZEBO_ROOT / 'maps' / 'small_house.yaml'
    )
    user_map = json.loads((
        PACKAGE_ROOT / 'maps' / 'small_house_user_map.geojson'
    ).read_text(encoding='utf-8'))
    zone_path = (
        PACKAGE_ROOT
        / 'maps'
        / f'{slam_map.map_id}-zones.geojson'
    )
    zones = load_zones(
        zone_path,
        slam_map.map_id,
        slam_map.map_revision,
        slam_map.legacy_map_ids,
    )
    mask = yaml.safe_load((
        PACKAGE_ROOT / 'maps' / 'zone-filter.yaml'
    ).read_text(encoding='utf-8'))

    assert user_map['map_id'] == slam_map.map_id
    assert user_map['map_revision'] == slam_map.map_revision
    assert len(zones) == 1
    assert zones[0]['properties']['behavior'] == 'restricted'
    assert mask['resolution'] == slam_map.transform.resolution
    assert mask['origin'] == [-12.5, -12.5, 0.0]


def test_room_routes_are_free_and_outside_the_generated_keepout_mask():
    slam_map = load_slam_map(
        GAZEBO_ROOT / 'maps' / 'small_house.yaml'
    )
    mask = cv2.imread(
        str(PACKAGE_ROOT / 'maps' / 'zone-filter.pgm'),
        cv2.IMREAD_GRAYSCALE,
    )
    _, rooms = load_room_routes(
        PACKAGE_ROOT / 'config' / 'room_routes.yaml'
    )

    for room in rooms:
        for waypoint in room.waypoints:
            pixel_x, pixel_y = slam_map.transform.pixel([
                waypoint.x, waypoint.y,
            ])
            assert slam_map.image[pixel_y, pixel_x] >= 250
            assert mask[pixel_y, pixel_x] == 0

    restricted_x, restricted_y = slam_map.transform.pixel([-4.7, 0.0])
    assert mask[restricted_y, restricted_x] == 100
