"""Contract tests for the selected ROSOrin hardware profile and URDF."""

import math
from pathlib import Path
from xml.etree import ElementTree

import xacro

from malbut_description.variant_config import (
    load_variant_arguments,
    xacro_value,
)


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROFILE = (
    PACKAGE_ROOT
    / 'config'
    / 'ultimate_orin_nx_super_mecanum.yaml'
)


def _render_robot():
    arguments = load_variant_arguments(PROFILE)
    mappings = {
        key: xacro_value(value) for key, value in arguments.items()
    }
    text = xacro.process_file(
        str(PACKAGE_ROOT / 'urdf' / 'rosorin.xacro'),
        mappings=mappings,
    ).toxml()
    return arguments, text, ElementTree.fromstring(text)


def test_selected_variant_and_component_mass_contract():
    """The selected NX variant has one complete, mass-balanced profile."""
    data = load_variant_arguments(PROFILE)
    assert math.isclose(data['total_mass'], 2.66)
    component_mass = (
        data['base_mass']
        + data['upper_body_mass']
        + 4 * data['wheel_mass']
        + data['camera_mass']
        + data['lidar_mass']
        + data['imu_mass']
        + data['microphone_mass']
    )
    assert math.isclose(component_mass, data['total_mass'], abs_tol=1e-9)
    profile_text = PROFILE.read_text(encoding='utf-8')
    assert '42123598463063' in profile_text
    assert 'Jetson Orin NX Super 8GB' in profile_text


def test_urdf_has_one_set_of_four_mecanum_wheels():
    """The URDF must not recreate the legacy fixed-plus-dynamic eight wheels."""
    _, text, robot = _render_robot()
    expected = {
        'front_left_wheel_joint',
        'front_right_wheel_joint',
        'rear_left_wheel_joint',
        'rear_right_wheel_joint',
    }
    continuous = {
        joint.get('name')
        for joint in robot.findall('joint')
        if joint.get('type') == 'continuous'
    }
    assert continuous == expected
    assert len(
        [
            link
            for link in robot.findall('link')
            if link.get('name', '').endswith('_wheel_link')
        ]
    ) == 4
    assert '<mesh' not in text


def test_sensor_frames_follow_the_published_interface_contract():
    """Required base, 360-degree lidar, IMU, and RGB-D frames exist."""
    _, _, robot = _render_robot()
    links = {link.get('name') for link in robot.findall('link')}
    assert {
        'base_footprint',
        'base_link',
        'laser_frame',
        'imu_link',
        'camera_link',
        'camera_depth_optical_frame',
    } <= links
