"""Static contracts for the Malbut Nav2 integration."""

import importlib.util
from pathlib import Path

from launch import LaunchContext
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.substitutions import LaunchConfiguration
from launch.utilities import perform_substitutions
import pytest
import yaml


REPOSITORY_ROOT = Path(__file__).parents[2]
DESCRIPTION_ROOT = REPOSITORY_ROOT / 'malbut_description'
GAZEBO_ROOT = REPOSITORY_ROOT / 'malbut_gazebo'
NAV2_PARAMS = GAZEBO_ROOT / 'config' / 'nav2_params.yaml'
SMALL_HOUSE_MAP = GAZEBO_ROOT / 'maps' / 'small_house.yaml'
ROBOT_PROFILE = (
    DESCRIPTION_ROOT
    / 'config'
    / 'ultimate_orin_nx_super_mecanum.yaml'
)


def _parameters(config, node_name):
    return config[node_name]['ros__parameters']


def test_nav2_uses_omnidirectional_motion_limits():
    """AMCL, DWB, and the smoother must all permit Mecanum strafing."""
    config = yaml.safe_load(NAV2_PARAMS.read_text(encoding='utf-8'))
    amcl = _parameters(config, 'amcl')
    controller = _parameters(config, 'controller_server')
    follow_path = controller['FollowPath']
    smoother = _parameters(config, 'velocity_smoother')

    assert amcl['robot_model_type'] == 'nav2_amcl::OmniMotionModel'
    assert controller['min_y_velocity_threshold'] < follow_path['max_vel_y']
    assert follow_path['min_vel_y'] < 0 < follow_path['max_vel_y']
    assert follow_path['acc_lim_y'] > 0
    assert follow_path['decel_lim_y'] < 0
    assert smoother['min_velocity'][1] < 0 < smoother['max_velocity'][1]
    assert smoother['max_accel'][1] > 0
    assert smoother['max_decel'][1] < 0


def test_costmap_footprints_cover_the_published_robot_envelope():
    """Both costmaps must cover the selected variant's length and width."""
    config = yaml.safe_load(NAV2_PARAMS.read_text(encoding='utf-8'))
    profile = yaml.safe_load(ROBOT_PROFILE.read_text(encoding='utf-8'))
    arguments = profile['xacro']['arguments']
    half_length = max(
        arguments['overall_length'] / 2.0,
        arguments['wheelbase'] / 2.0 + arguments['wheel_radius'],
    )
    half_width = max(
        arguments['overall_width'] / 2.0,
        arguments['wheel_separation'] / 2.0
        + arguments['wheel_radius'],
    )

    footprints = []
    for costmap_name in ('local_costmap', 'global_costmap'):
        costmap = config[costmap_name][costmap_name]['ros__parameters']
        assert 'robot_radius' not in costmap
        footprints.append(yaml.safe_load(costmap['footprint']))

    assert footprints[0] == footprints[1]
    x_values = [point[0] for point in footprints[0]]
    y_values = [point[1] for point in footprints[0]]
    assert min(x_values) <= -half_length
    assert max(x_values) >= half_length
    assert min(y_values) <= -half_width
    assert max(y_values) >= half_width


def test_navigation_has_one_public_upstream_bringup_entry_point():
    """The public launch delegates both process modes to Nav2 bringup."""
    launch_file = GAZEBO_ROOT / 'launch' / 'navigation.launch.py'
    legacy_include = GAZEBO_ROOT / 'launch' / 'include' / launch_file.name

    assert launch_file.is_file()
    assert not legacy_include.exists()

    spec = importlib.util.spec_from_file_location(
        'malbut_navigation_launch', launch_file
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    description = module.generate_launch_description()

    declared_arguments = {
        entity.name
        for entity in description.entities
        if isinstance(entity, DeclareLaunchArgument)
    }
    assert 'use_composition' in declared_arguments
    launch_text = launch_file.read_text(encoding='utf-8')
    assert "'maps', 'small_house.yaml'" in launch_text
    assert "'maps', 'map_01.yaml'" not in launch_text

    includes = [
        entity
        for entity in description.entities
        if isinstance(entity, IncludeLaunchDescription)
    ]
    context = LaunchContext()
    for include in includes:
        include.launch_description_source.get_launch_description(context)

    bringup = next(
        include
        for include in includes
        if include.launch_description_source.location.endswith(
            '/nav2_bringup/launch/bringup_launch.py'
        )
    )
    launch_arguments = dict(bringup.launch_arguments)
    composition_argument = launch_arguments['use_composition']
    assert isinstance(composition_argument, LaunchConfiguration)
    assert perform_substitutions(
        context, composition_argument.variable_name
    ) == 'use_composition'


def test_small_house_map_covers_the_full_aws_world():
    """The default saved map must be the full-size Small House grid."""
    config = yaml.safe_load(SMALL_HOUSE_MAP.read_text(encoding='utf-8'))
    image = SMALL_HOUSE_MAP.parent / config['image']

    assert image.name == 'small_house.pgm'
    assert image.is_file()
    assert config['resolution'] == 0.05
    assert config['origin'] == [-12.5, -12.5, 0.0]
    with image.open('rb') as stream:
        assert stream.readline().strip() == b'P5'
        while True:
            dimensions = stream.readline()
            if not dimensions.startswith(b'#'):
                break
    assert dimensions.split() == [b'500', b'500']


def test_readme_documents_real_navigation_and_teleop_interfaces():
    """User-facing commands must match implemented launch and key controls."""
    readme_path = REPOSITORY_ROOT / 'README.md'
    if not readme_path.is_file():
        pytest.skip('repository README is not part of the package layout')
    readme = readme_path.read_text(encoding='utf-8')

    assert 'ros2 launch malbut_gazebo navigation.launch.py' in readme
    assert '`q`/`e`' in readme
    assert 'DEPTH_CAMERA_TYPE' not in readme
