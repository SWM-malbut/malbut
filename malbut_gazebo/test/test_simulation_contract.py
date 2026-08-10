"""Generated-model contracts for Gazebo Fortress and the ROS bridge."""

import math
from pathlib import Path
from xml.etree import ElementTree

import pytest
import xacro
import yaml

from malbut_description.variant_config import (
    load_variant_arguments,
    xacro_value,
)


GAZEBO_ROOT = Path(__file__).resolve().parents[1]
DESCRIPTION_ROOT = GAZEBO_ROOT.parent / 'malbut_description'
PROFILE = DESCRIPTION_ROOT / 'config' / 'rosorin_ultimate_mecanum.yaml'
IGNITION_NAMESPACE = 'http://ignitionrobotics.org/schema'


def _render_simulation_robot(**overrides):
    arguments = load_variant_arguments(PROFILE)
    mappings = {
        key: xacro_value(value) for key, value in arguments.items()
    }
    mappings.update({
        key: xacro_value(value) for key, value in overrides.items()
    })
    text = xacro.process_file(
        str(GAZEBO_ROOT / 'urdf' / 'robot.gazebo.xacro'),
        mappings=mappings,
    ).toxml()
    return arguments, ElementTree.fromstring(text)


def _plugins(root):
    return {
        plugin.get('name'): plugin
        for plugin in root.findall('.//plugin')
    }


def _sensors(root):
    return {
        sensor.get('name'): sensor
        for sensor in root.findall('.//sensor')
    }


def test_mecanum_drive_keeps_each_joint_in_the_correct_corner():
    """A set comparison is insufficient: every plugin slot must be correct."""
    arguments, robot = _render_simulation_robot()
    drive = _plugins(robot)['ignition::gazebo::systems::MecanumDrive']
    assert drive.get('filename') == 'ignition-gazebo-mecanum-drive-system'
    assert {
        'front_left_joint': 'front_left_wheel_joint',
        'front_right_joint': 'front_right_wheel_joint',
        'back_left_joint': 'rear_left_wheel_joint',
        'back_right_joint': 'rear_right_wheel_joint',
    } == {
        tag: drive.findtext(tag)
        for tag in (
            'front_left_joint',
            'front_right_joint',
            'back_left_joint',
            'back_right_joint',
        )
    }
    assert drive.findtext('topic') == '/cmd_vel'
    for tag, profile_name in {
        'wheel_separation': 'wheel_separation',
        'wheelbase': 'wheelbase',
        'wheel_radius': 'wheel_radius',
        'min_acceleration': 'drive_min_acceleration',
        'max_acceleration': 'drive_max_acceleration',
        'min_velocity': 'drive_min_velocity',
        'max_velocity': 'drive_max_velocity',
    }.items():
        assert float(drive.findtext(tag)) == pytest.approx(
            arguments[profile_name]
        )


def test_mecanum_contact_directions_match_the_abab_wheel_layout():
    """Directional friction must agree with the four handed wheel meshes."""
    arguments, robot = _render_simulation_robot()
    expected = {
        'front_left_wheel_link': (1.0, -1.0, 0.0),
        'front_right_wheel_link': (1.0, 1.0, 0.0),
        'rear_left_wheel_link': (1.0, 1.0, 0.0),
        'rear_right_wheel_link': (1.0, -1.0, 0.0),
    }
    gazebos = {
        gazebo.get('reference'): gazebo
        for gazebo in robot.findall('gazebo')
        if gazebo.get('reference') in expected
    }
    assert set(gazebos) == set(expected)
    for link, direction in expected.items():
        friction = gazebos[link].find('collision/surface/friction/ode')
        assert friction is not None
        assert float(friction.findtext('mu')) == pytest.approx(
            arguments['wheel_surface_mu1']
        )
        assert float(friction.findtext('mu2')) == pytest.approx(
            arguments['wheel_surface_mu2']
        )
        fdir = friction.find('fdir1')
        assert tuple(float(value) for value in fdir.text.split()) == direction
        assert fdir.get(
            f'{{{IGNITION_NAMESPACE}}}expressed_in'
        ) == 'base_footprint'


def test_odometry_and_joint_state_plugins_publish_the_model_contract():
    """Nav2 and robot_state_publisher require stable frames and joint names."""
    arguments, robot = _render_simulation_robot()
    plugins = _plugins(robot)
    odometry = plugins['ignition::gazebo::systems::OdometryPublisher']
    assert odometry.get(
        'filename'
    ) == 'ignition-gazebo-odometry-publisher-system'
    assert odometry.findtext('odom_frame') == 'odom'
    assert odometry.findtext('robot_base_frame') == 'base_footprint'
    assert odometry.findtext('dimensions') == '2'
    assert odometry.findtext('odom_topic') == '/odom'
    assert odometry.findtext('tf_topic') == '/tf'
    assert float(odometry.findtext('odom_publish_frequency')) == pytest.approx(
        arguments['odom_publish_frequency']
    )

    joint_states = plugins['ignition::gazebo::systems::JointStatePublisher']
    assert joint_states.findtext('topic') == '/joint_states'
    assert [
        element.text for element in joint_states.findall('joint_name')
    ] == [
        'front_left_wheel_joint',
        'front_right_wheel_joint',
        'rear_left_wheel_joint',
        'rear_right_wheel_joint',
    ]


def test_lidar_camera_and_imu_match_the_selected_profile():
    """Rendered sensor rates, ranges, frames, and image geometry use the YAML."""
    arguments, robot = _render_simulation_robot()
    sensors = _sensors(robot)
    assert set(sensors) == {
        'stl19p_d500',
        'aurora930_pro',
        'controller_imu',
    }

    lidar = sensors['stl19p_d500']
    assert lidar.get('type') == 'gpu_lidar'
    assert lidar.findtext('topic') == '/scan'
    assert lidar.findtext('gz_frame_id') == 'laser_frame'
    assert float(lidar.findtext('update_rate')) == pytest.approx(
        arguments['lidar_rate']
    )
    assert int(lidar.findtext('.//horizontal/samples')) == int(
        arguments['lidar_samples']
    )
    assert float(lidar.findtext('.//horizontal/min_angle')) == pytest.approx(
        -math.pi
    )
    assert float(lidar.findtext('.//horizontal/max_angle')) == pytest.approx(
        math.pi
    )
    for tag, name in {
        'min': 'lidar_min_range',
        'max': 'lidar_max_range',
        'resolution': 'lidar_range_resolution',
    }.items():
        assert float(lidar.findtext(f'.//range/{tag}')) == pytest.approx(
            arguments[name]
        )
    assert float(lidar.findtext('.//noise/stddev')) == pytest.approx(
        arguments['lidar_noise_stddev']
    )

    camera = sensors['aurora930_pro']
    assert camera.get('type') == 'rgbd_camera'
    assert camera.findtext('topic') == '/rgbd_camera'
    assert camera.findtext('gz_frame_id') == 'camera_link'
    assert float(camera.findtext('update_rate')) == pytest.approx(
        arguments['camera_rate']
    )
    assert float(camera.findtext('./camera/horizontal_fov')) == pytest.approx(
        arguments['camera_hfov']
    )
    assert int(camera.findtext('./camera/image/width')) == int(
        arguments['camera_width']
    )
    assert int(camera.findtext('./camera/image/height')) == int(
        arguments['camera_height']
    )
    assert float(camera.findtext('./camera/clip/near')) == pytest.approx(
        arguments['camera_near']
    )
    assert float(camera.findtext('./camera/clip/far')) == pytest.approx(
        arguments['camera_far']
    )
    assert float(
        camera.findtext('./camera/depth_camera/clip/near')
    ) == pytest.approx(arguments['depth_camera_near'])
    assert float(
        camera.findtext('./camera/depth_camera/clip/far')
    ) == pytest.approx(arguments['depth_camera_far'])

    imu = sensors['controller_imu']
    assert imu.get('type') == 'imu'
    assert imu.findtext('topic') == '/imu'
    assert imu.findtext('gz_frame_id') == 'imu_link'
    assert float(imu.findtext('update_rate')) == pytest.approx(
        arguments['imu_rate']
    )


@pytest.mark.parametrize(
    ('argument', 'sensor_name'),
    [
        ('lidar_enabled', 'stl19p_d500'),
        ('depth_camera_enabled', 'aurora930_pro'),
        ('imu_enabled', 'controller_imu'),
    ],
)
def test_each_sensor_launch_switch_removes_only_that_sensor(
    argument,
    sensor_name,
):
    """Sensor launch flags must affect the generated model, not just the GUI."""
    _arguments, enabled = _render_simulation_robot()
    _arguments, disabled = _render_simulation_robot(**{argument: False})
    enabled_names = set(_sensors(enabled))
    disabled_names = set(_sensors(disabled))
    assert disabled_names == enabled_names - {sensor_name}


def test_lidar_scan_plane_is_above_the_chassis_envelope():
    """A correctly mounted scan plane must not cut through the body mesh."""
    arguments, robot = _render_simulation_robot()
    lidar = _sensors(robot)['stl19p_d500']
    assert tuple(float(value) for value in lidar.findtext('pose').split()) == (
        0.0,
    ) * 6
    scan_plane_z = arguments['base_footprint_z'] + arguments['lidar_z']
    body_top = arguments['base_footprint_z'] + arguments['base_height']
    assert scan_plane_z > body_top


def test_bridge_defines_the_complete_ros_gazebo_interface():
    """Every public topic must retain its transport names, types, QoS, and flow."""
    bridge = yaml.safe_load(
        (GAZEBO_ROOT / 'config' / 'bridge.yaml').read_text(encoding='utf-8')
    )
    mappings = {entry['ros_topic_name']: entry for entry in bridge}
    assert len(mappings) == len(bridge)
    expected = {
        '/clock': ('/clock', 'rosgraph_msgs/msg/Clock', 'gz.msgs.Clock', 'GZ_TO_ROS', 'CLOCK'),
        '/cmd_vel': ('/cmd_vel', 'geometry_msgs/msg/Twist', 'gz.msgs.Twist', 'ROS_TO_GZ', None),
        '/odom': ('/odom', 'nav_msgs/msg/Odometry', 'gz.msgs.Odometry', 'GZ_TO_ROS', None),
        '/tf': ('/tf', 'tf2_msgs/msg/TFMessage', 'gz.msgs.Pose_V', 'GZ_TO_ROS', None),
        '/joint_states': (
            '/joint_states', 'sensor_msgs/msg/JointState',
            'gz.msgs.Model', 'GZ_TO_ROS', None,
        ),
        '/scan': (
            '/scan', 'sensor_msgs/msg/LaserScan',
            'gz.msgs.LaserScan', 'GZ_TO_ROS', 'SENSOR_DATA',
        ),
        '/imu/data': ('/imu', 'sensor_msgs/msg/Imu', 'gz.msgs.IMU', 'GZ_TO_ROS', 'SENSOR_DATA'),
        '/camera/color/image_raw': (
            '/rgbd_camera/image', 'sensor_msgs/msg/Image',
            'gz.msgs.Image', 'GZ_TO_ROS', 'SENSOR_DATA',
        ),
        '/camera/color/camera_info': (
            '/rgbd_camera/camera_info', 'sensor_msgs/msg/CameraInfo',
            'gz.msgs.CameraInfo', 'GZ_TO_ROS', 'SENSOR_DATA',
        ),
        '/camera/depth/image_raw': (
            '/rgbd_camera/depth_image', 'sensor_msgs/msg/Image',
            'gz.msgs.Image', 'GZ_TO_ROS', 'SENSOR_DATA',
        ),
        '/camera/depth/points': (
            '/rgbd_camera/points', 'sensor_msgs/msg/PointCloud2',
            'gz.msgs.PointCloudPacked', 'GZ_TO_ROS', 'SENSOR_DATA',
        ),
    }
    assert set(mappings) == set(expected)
    for topic, values in expected.items():
        mapping = mappings[topic]
        actual = (
            mapping['gz_topic_name'],
            mapping['ros_type_name'],
            mapping['gz_type_name'],
            mapping['direction'],
            mapping.get('qos_profile'),
        )
        assert actual == values


def test_world_guis_expose_camera_controls_and_mecanum_teleop():
    """Every distributed world must remain operable from the Gazebo window."""
    required_plugins = {
        'MinimalScene',
        'EntityContextMenuPlugin',
        'GzSceneManager',
        'InteractiveViewControl',
        'CameraTracking',
        'SelectEntities',
        'VisualizationCapabilities',
        'TransformControl',
        'WorldControl',
        'Teleop',
        'EntityTree',
        'ComponentInspector',
    }
    for world_file in sorted((GAZEBO_ROOT / 'worlds').glob('*.sdf')):
        root = ElementTree.parse(world_file).getroot()
        gui = root.find('world/gui')
        assert gui is not None, world_file.name
        plugins = {
            plugin.get('filename'): plugin for plugin in gui.findall('plugin')
        }
        assert required_plugins <= plugins.keys(), world_file.name
        assert plugins['Teleop'].findtext('topic') == '/cmd_vel'
