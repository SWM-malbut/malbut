"""Static contracts for the Malbut Nav2 integration."""

import importlib.util
import math
from pathlib import Path

from launch import LaunchContext
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.substitutions import LaunchConfiguration
from launch.utilities import perform_substitutions
from launch_ros.actions import Node
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
    / 'rosorin_ultimate_mecanum.yaml'
)


def _parameters(config, node_name):
    return config[node_name]['ros__parameters']


def test_nav2_uses_omnidirectional_motion_limits():
    """AMCL, MPPI, and the smoother must all permit Mecanum strafing."""
    config = yaml.safe_load(NAV2_PARAMS.read_text(encoding='utf-8'))
    amcl = _parameters(config, 'amcl')
    controller = _parameters(config, 'controller_server')
    follow_path = controller['FollowPath']
    smoother = _parameters(config, 'velocity_smoother')

    assert amcl['robot_model_type'] == 'nav2_amcl::OmniMotionModel'
    assert controller['min_y_velocity_threshold'] < follow_path['vy_max']
    assert follow_path['vy_max'] > 0
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
    assert 'zone_mask' in declared_arguments
    assert 'localization_source' in declared_arguments
    assert 'localization_state' in declared_arguments
    assert 'robot_web' in declared_arguments
    assert 'robot_web_port' in declared_arguments
    assert 'user_map' in declared_arguments
    assert 'set_initial_pose' in declared_arguments
    assert 'initial_pose_x' in declared_arguments
    assert 'initial_pose_y' in declared_arguments
    assert 'initial_pose_yaw' in declared_arguments

    robot_web_nodes = [
        entity
        for entity in description.entities
        if isinstance(entity, Node)
        and entity.node_executable == 'robot_web_server'
    ]
    assert len(robot_web_nodes) == 1

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
    assert any(
        include.launch_description_source.location.endswith(
            '/nav2_bringup/launch/navigation_launch.py'
        )
        for include in includes
    )


def test_zone_mask_enables_both_costmap_filters_and_support_nodes():
    """One optional mask must constrain planning and local control."""
    config = yaml.safe_load(NAV2_PARAMS.read_text(encoding='utf-8'))
    for costmap_name in ('local_costmap', 'global_costmap'):
        costmap = config[costmap_name][costmap_name]['ros__parameters']
        assert costmap['filters'] == ['keepout_filter']
        assert costmap['keepout_filter'] == {
            'plugin': 'nav2_costmap_2d::KeepoutFilter',
            'enabled': False,
            'filter_info_topic': '/keepout_costmap_filter_info',
        }
        assert 'keepout_inflation' not in costmap

    launch_file = GAZEBO_ROOT / 'launch' / 'navigation.launch.py'
    spec = importlib.util.spec_from_file_location(
        'malbut_navigation_zone_launch', launch_file
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    description = module.generate_launch_description()

    node_names = {
        entity._Node__node_name
        for entity in description.entities
        if isinstance(entity, Node)
    }
    assert {
        'zone_filter_mask_server',
        'zone_filter_info_server',
        'zone_filter_lifecycle_manager',
        'localization_state_restorer',
    } <= node_names


def test_navigation_prefers_clearance_and_keeps_close_obstacles_visible():
    """Planning and control must preserve a safety margin around obstacles."""
    config = yaml.safe_load(NAV2_PARAMS.read_text(encoding='utf-8'))
    controller = config['controller_server']['ros__parameters']
    progress = controller['progress_checker']
    assert progress['required_movement_radius'] <= 0.15
    assert progress['movement_time_allowance'] >= 30.0
    follow_path = controller['FollowPath']
    assert follow_path['plugin'] == 'nav2_mppi_controller::MPPIController'
    assert follow_path['motion_model'] == 'Omni'
    assert follow_path['model_dt'] == 1.0 / controller['controller_frequency']
    assert follow_path['time_steps'] * follow_path['model_dt'] >= 2.5
    assert follow_path['batch_size'] >= 1000
    assert follow_path['vx_max'] > 0.0
    assert abs(follow_path['vy_max']) <= 0.1
    assert follow_path['wz_max'] <= 0.4
    assert follow_path['CostCritic']['consider_footprint'] is True
    assert follow_path['PathAngleCritic']['forward_preference'] is True
    assert follow_path['PreferForwardCritic']['enabled'] is True
    assert follow_path['TwirlingCritic']['enabled'] is True
    person_controller = controller['FollowPerson']
    assert person_controller['plugin'] == (
        'nav2_mppi_controller::MPPIController'
    )
    assert person_controller['motion_model'] == 'Omni'
    assert person_controller['vx_min'] == follow_path['vx_min']
    assert person_controller['wz_max'] <= 0.001
    assert person_controller['wz_std'] <= 0.001
    assert 'GoalAngleCritic' not in person_controller['critics']
    assert 'PreferForwardCritic' not in person_controller['critics']
    assert 'TwirlingCritic' not in person_controller['critics']
    assert controller['general_goal_checker']['xy_goal_tolerance'] <= 0.05
    assert controller['general_goal_checker']['yaw_goal_tolerance'] >= 3.14

    smoother = config['velocity_smoother']['ros__parameters']
    assert smoother['feedback'] == 'CLOSED_LOOP'
    assert smoother['max_velocity'] == [0.4, 0.1, 0.4]

    planner = config['planner_server']['ros__parameters']['GridBased']
    assert planner['plugin'] == 'nav2_smac_planner/SmacPlanner2D'
    assert planner['cost_travel_multiplier'] >= 4.0
    assert planner['tolerance'] <= controller[
        'general_goal_checker'
    ]['xy_goal_tolerance']
    assert planner['downsample_costmap'] is False
    assert planner['allow_unknown'] is False

    profile = yaml.safe_load(ROBOT_PROFILE.read_text(encoding='utf-8'))
    camera = profile['xacro']['arguments']
    expected_vertical_fov = 2.0 * math.atan(
        math.tan(camera['camera_hfov'] / 2.0)
        * camera['camera_height']
        / camera['camera_width']
    )

    for costmap_name in ('local_costmap', 'global_costmap'):
        costmap = config[costmap_name][costmap_name]['ros__parameters']
        assert 0.04 <= costmap['footprint_padding'] <= 0.06
        inflation = costmap['inflation_layer']
        assert inflation['inflation_radius'] >= 0.55
        assert inflation['cost_scaling_factor'] <= 3.0

        scan_layer = costmap['obstacle_layer']
        assert scan_layer['plugin'] == 'nav2_costmap_2d::ObstacleLayer'
        assert scan_layer['observation_sources'] == 'scan'
        scan = scan_layer['scan']
        assert 0.19 <= scan['obstacle_min_range'] <= 0.21
        assert scan['raytrace_min_range'] == 0.0

        depth_layer = costmap['depth_voxel_layer']
        assert depth_layer['plugin'] == (
            'spatio_temporal_voxel_layer/SpatioTemporalVoxelLayer'
        )
        assert 0.0 < depth_layer['voxel_decay'] <= 3.0
        assert depth_layer['decay_model'] == 0
        assert depth_layer['mapping_mode'] is False
        assert set(depth_layer['observation_sources'].split()) == {
            'depth_mark', 'depth_clear'
        }

        depth_mark = depth_layer['depth_mark']
        assert depth_mark['topic'] == '/camera/depth/points'
        assert depth_mark['data_type'] == 'PointCloud2'
        assert depth_mark['marking'] is True
        assert depth_mark['clearing'] is False
        assert 0.03 <= depth_mark['min_obstacle_height'] <= 0.10
        assert depth_mark['obstacle_range'] <= 2.50
        assert depth_mark['observation_persistence'] == 0.0
        assert depth_mark['clear_after_reading'] is True

        depth_clear = depth_layer['depth_clear']
        assert depth_clear['topic'] == '/camera/depth/points'
        assert depth_clear['data_type'] == 'PointCloud2'
        assert depth_clear['marking'] is False
        assert depth_clear['clearing'] is True
        assert depth_clear['model_type'] == 0
        assert depth_clear['sensor_frame'] == (
            'camera_depth_optical_frame'
        )
        assert depth_clear['min_z'] == camera['depth_camera_near']
        assert depth_clear['max_z'] == camera['depth_camera_far']
        assert depth_clear['vertical_fov_angle'] == pytest.approx(
            expected_vertical_fov
        )
        assert depth_clear['horizontal_fov_angle'] == pytest.approx(
            camera['camera_hfov']
        )
        assert depth_clear['decay_acceleration'] > 0.0


def test_zone_filter_is_disabled_without_a_mask_and_enabled_with_one():
    """Existing navigation must remain unchanged until a mask is passed."""
    launch_file = GAZEBO_ROOT / 'launch' / 'navigation.launch.py'
    spec = importlib.util.spec_from_file_location(
        'malbut_navigation_zone_params', launch_file
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    description = module.generate_launch_description()
    location_context = LaunchContext()
    includes = [
        entity
        for entity in description.entities
        if isinstance(entity, IncludeLaunchDescription)
    ]
    for include in includes:
        include.launch_description_source.get_launch_description(
            location_context
        )
    bringup = next(
        entity
        for entity in includes
        if entity.launch_description_source.location.endswith(
            '/nav2_bringup/launch/bringup_launch.py'
        )
    )
    configured_params = dict(bringup.launch_arguments)['params_file']

    for zone_mask, expected in (
        ('', False),
        ('/tmp/zones.yaml', True),
    ):
        context = LaunchContext()
        context.launch_configurations['namespace'] = ''
        context.launch_configurations['params_file'] = str(NAV2_PARAMS)
        context.launch_configurations['zone_mask'] = zone_mask
        rewritten_path = perform_substitutions(
            context, [configured_params]
        )
        rewritten = yaml.safe_load(Path(rewritten_path).read_text())
        for costmap_name in ('local_costmap', 'global_costmap'):
            costmap = rewritten[costmap_name][costmap_name][
                'ros__parameters'
            ]
            assert costmap['keepout_filter']['enabled'] is expected
            assert 'keepout_inflation' not in costmap
            assert costmap['keepout_filter']['filter_info_topic'] == (
                '/keepout_costmap_filter_info'
            )


def test_zone_filter_topics_follow_the_navigation_namespace():
    """Filter servers and costmaps must resolve one absolute topic."""
    launch_file = GAZEBO_ROOT / 'launch' / 'navigation.launch.py'
    spec = importlib.util.spec_from_file_location(
        'malbut_navigation_zone_namespace', launch_file
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    description = module.generate_launch_description()
    includes = [
        entity
        for entity in description.entities
        if isinstance(entity, IncludeLaunchDescription)
    ]
    location_context = LaunchContext()
    for include in includes:
        include.launch_description_source.get_launch_description(
            location_context
        )
    bringup = next(
        entity
        for entity in includes
        if entity.launch_description_source.location.endswith(
            '/nav2_bringup/launch/bringup_launch.py'
        )
    )
    configured_params = dict(bringup.launch_arguments)['params_file']
    context = LaunchContext()
    context.launch_configurations.update({
        'namespace': 'robot_1',
        'params_file': str(NAV2_PARAMS),
        'zone_mask': '/tmp/zones.yaml',
    })
    rewritten_path = perform_substitutions(context, [configured_params])
    rewritten = yaml.safe_load(Path(rewritten_path).read_text())
    for costmap_name in ('local_costmap', 'global_costmap'):
        costmap = rewritten[costmap_name][costmap_name][
            'ros__parameters'
        ]
        assert costmap['keepout_filter']['filter_info_topic'] == (
            '/robot_1/keepout_costmap_filter_info'
        )


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
