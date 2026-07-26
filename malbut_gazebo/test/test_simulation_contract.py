"""Contract tests for the Fortress robot plugins and ROS bridge."""

from pathlib import Path
from xml.etree import ElementTree

import xacro
import yaml

from malbut_description.variant_config import (
    load_variant_arguments,
    xacro_value,
)


GAZEBO_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = GAZEBO_ROOT.parent
DESCRIPTION_ROOT = REPOSITORY_ROOT / 'malbut_description'
PROFILE = (
    DESCRIPTION_ROOT
    / 'config'
    / 'ultimate_orin_nx_super_mecanum.yaml'
)


def _render_simulation_robot():
    arguments = load_variant_arguments(PROFILE)
    mappings = {
        key: xacro_value(value) for key, value in arguments.items()
    }
    text = xacro.process_file(
        str(GAZEBO_ROOT / 'urdf' / 'robot.gazebo.xacro'),
        mappings=mappings,
    ).toxml()
    return text, ElementTree.fromstring(text)


def test_mecanum_plugin_uses_the_four_model_joints_and_cmd_vel():
    """The drive plugin matches the only four wheel joints in the model."""
    _, robot = _render_simulation_robot()
    plugins = {
        plugin.get('name'): plugin
        for plugin in robot.findall('.//plugin')
    }
    drive = plugins['ignition::gazebo::systems::MecanumDrive']
    assert drive.findtext('topic') == '/cmd_vel'
    assert {
        drive.findtext('front_left_joint'),
        drive.findtext('front_right_joint'),
        drive.findtext('back_left_joint'),
        drive.findtext('back_right_joint'),
    } == {
        'front_left_wheel_joint',
        'front_right_wheel_joint',
        'rear_left_wheel_joint',
        'rear_right_wheel_joint',
    }


def test_sensor_plugins_match_selected_hardware_baseline():
    """The simulator exposes 360-degree lidar, RGB-D, and IMU sensors."""
    _, robot = _render_simulation_robot()
    sensors = {
        sensor.get('name'): sensor
        for sensor in robot.findall('.//sensor')
    }
    assert set(sensors) == {
        'stl19p_d500',
        'aurora930_pro',
        'controller_imu',
    }
    lidar = sensors['stl19p_d500']
    assert lidar.get('type') == 'gpu_lidar'
    assert lidar.findtext('topic') == '/scan'
    assert float(lidar.findtext('.//min_angle')) < -3.14
    assert float(lidar.findtext('.//max_angle')) > 3.14
    assert sensors['aurora930_pro'].get('type') == 'rgbd_camera'
    assert sensors['controller_imu'].findtext('topic') == '/imu'


def test_bridge_has_one_canonical_mapping_per_ros_topic():
    """The bridge publishes the documented ROS-facing topic contract."""
    bridge = yaml.safe_load(
        (GAZEBO_ROOT / 'config' / 'bridge.yaml').read_text(
            encoding='utf-8'
        )
    )
    mappings = {entry['ros_topic_name']: entry for entry in bridge}
    assert len(mappings) == len(bridge)
    assert set(mappings) == {
        '/clock',
        '/cmd_vel',
        '/odom',
        '/tf',
        '/joint_states',
        '/scan',
        '/imu/data',
        '/camera/color/image_raw',
        '/camera/color/camera_info',
        '/camera/depth/image_raw',
        '/camera/depth/points',
    }
    assert mappings['/cmd_vel']['direction'] == 'ROS_TO_GZ'
    assert all(
        entry['direction'] == 'GZ_TO_ROS'
        for topic, entry in mappings.items()
        if topic != '/cmd_vel'
    )


def test_world_guis_load_scene_controls_and_teleop():
    """Bundled GUI worlds render entities and expose in-window controls."""
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
    for filename in ('empty.sdf', 'test_arena.sdf', 'small_house.sdf'):
        root = ElementTree.parse(
            GAZEBO_ROOT / 'worlds' / filename
        ).getroot()
        gui_elements = root.findall('.//gui')
        assert len(gui_elements) == 1
        plugin_elements = gui_elements[0].findall('plugin')
        plugin_filenames = [
            plugin.get('filename') for plugin in plugin_elements
        ]
        assert all(plugin_filenames)
        assert len(plugin_filenames) == len(set(plugin_filenames))
        plugins = dict(zip(plugin_filenames, plugin_elements))
        assert required_plugins <= plugins.keys()
        assert plugin_filenames.index(
            'MinimalScene'
        ) < plugin_filenames.index(
            'GzSceneManager'
        ) < plugin_filenames.index(
            'InteractiveViewControl'
        )
        teleop = root.find(".//gui/plugin[@filename='Teleop']")
        assert teleop is not None
        assert teleop.findtext('topic') == '/cmd_vel'


def test_standalone_spawn_default_matches_the_empty_world_name():
    """The compatibility spawn entry point targets the bundled empty world."""
    world = ElementTree.parse(
        GAZEBO_ROOT / 'worlds' / 'empty.sdf'
    ).getroot().find('world')
    assert world is not None
    launch_text = (
        GAZEBO_ROOT / 'launch' / 'spawn_model.launch.py'
    ).read_text(encoding='utf-8')
    expected = (
        "DeclareLaunchArgument('world_name', "
        f"default_value='{world.get('name')}')"
    )
    assert expected in launch_text
