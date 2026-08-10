"""Contract tests for the selected ROSOrin hardware profile and URDF."""

import hashlib
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
OFFICIAL_MESHES = {
    'base_link.stl',
    'base_link_collision.stl',
    'dabai_cam_link.STL',
    'dabai_cam_link_collision.stl',
    'depth_camera_link.stl',
    'depth_camera_link_collision.stl',
    'left_back_wheel_link.stl',
    'left_back_wheel_link_collision.stl',
    'left_front_wheel_link.stl',
    'left_front_wheel_link_collision.stl',
    'lidar_link.stl',
    'lidar_link_collision.stl',
    'mic_link.stl',
    'mic_link_collision.stl',
    'right_back_wheel_link.stl',
    'right_back_wheel_link_collision.stl',
    'right_front_wheel_link.stl',
    'right_front_wheel_link_collision.stl',
}


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
    """Official wheel meshes belong to the four dynamic wheel links."""
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
    referenced = {
        Path(mesh.get('filename')).name
        for mesh in robot.findall('.//mesh')
    }
    assert {
        'left_front_wheel_link.stl',
        'right_front_wheel_link.stl',
        'left_back_wheel_link.stl',
        'right_back_wheel_link.stl',
    } <= referenced


def test_official_mesh_set_is_complete_and_used_by_the_robot():
    """The Hiwonder feature-package meshes are preserved, not recreated."""
    mesh_root = PACKAGE_ROOT / 'meshes'
    actual = {
        path.name
        for path in mesh_root.iterdir()
        if path.is_file() and path.suffix.lower() == '.stl'
    }
    assert actual == OFFICIAL_MESHES
    assert (mesh_root / 'SOURCE.md').is_file()

    _, _, robot = _render_robot()
    referenced = {
        Path(mesh.get('filename')).name
        for mesh in robot.findall('.//mesh')
    }
    assert {
        'base_link.stl',
        'base_link_collision.stl',
        'depth_camera_link.stl',
        'depth_camera_link_collision.stl',
        'lidar_link.stl',
        'lidar_link_collision.stl',
        'mic_link.stl',
        'mic_link_collision.stl',
    } <= referenced

    xacro_sources = '\n'.join(
        path.read_text(encoding='utf-8')
        for path in (PACKAGE_ROOT / 'urdf').rglob('*.xacro')
    )
    assert '/home/' not in xacro_sources
    assert '/Users/' not in xacro_sources


def test_official_mesh_checksums_are_unchanged():
    """Every vendored STL remains byte-identical to the source package."""
    mesh_root = PACKAGE_ROOT / 'meshes'
    expected = {}
    for line in (mesh_root / 'SHA256SUMS').read_text(
        encoding='utf-8'
    ).splitlines():
        digest, filename = line.split(maxsplit=1)
        expected[filename] = digest
    assert set(expected) == OFFICIAL_MESHES
    for filename, digest in expected.items():
        actual = hashlib.sha256(
            (mesh_root / filename).read_bytes()
        ).hexdigest()
        assert actual == digest


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
        'microphone_link',
    } <= links
