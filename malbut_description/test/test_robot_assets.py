"""Structural and physical contracts for the selected ROSOrin model."""

import hashlib
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


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROFILE = PACKAGE_ROOT / 'config' / 'rosorin_ultimate_mecanum.yaml'
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
    return arguments, ElementTree.fromstring(text)


def _named(root, tag, name):
    element = root.find(f"{tag}[@name='{name}']")
    assert element is not None, f'missing {tag} {name}'
    return element


def _vector(text):
    assert text is not None
    return tuple(float(value) for value in text.split())


def _joint_links(joint):
    parent = joint.find('parent')
    child = joint.find('child')
    assert parent is not None
    assert child is not None
    return parent.get('link'), child.get('link')


def test_profile_identifies_the_selected_physical_robot():
    """The active profile must name the purchased kit and sensor set."""
    profile = yaml.safe_load(PROFILE.read_text(encoding='utf-8'))
    assert profile['variant'] == {
        'id': '42123598463063',
        'sku': '21031708',
        'kit': 'Ultimate Kit',
        'compute': 'Jetson Orin NX Super 8GB',
        'chassis': 'ROSOrin Mecanum',
        'depth_camera': 'Aurora930 Pro',
        'lidar': 'STL-19P D500',
        'microphone': 'six-channel array',
    }


def test_robot_has_one_connected_link_and_joint_graph():
    """The generated URDF must expose exactly one copy of every component."""
    _arguments, robot = _render_robot()
    links = {link.get('name') for link in robot.findall('link')}
    assert links == {
        'base_footprint',
        'base_link',
        'front_left_wheel_link',
        'front_right_wheel_link',
        'rear_left_wheel_link',
        'rear_right_wheel_link',
        'camera_link',
        'camera_depth_optical_frame',
        'laser_frame',
        'imu_link',
        'microphone_link',
    }
    assert {joint.get('name') for joint in robot.findall('joint')} == {
        'base_footprint_joint',
        'front_left_wheel_joint',
        'front_right_wheel_joint',
        'rear_left_wheel_joint',
        'rear_right_wheel_joint',
        'camera_joint',
        'camera_depth_optical_joint',
        'laser_joint',
        'imu_joint',
        'microphone_joint',
    }

    children_by_parent = {name: [] for name in links}
    child_links = []
    for joint in robot.findall('joint'):
        parent, child = _joint_links(joint)
        assert parent in links
        assert child in links
        assert parent != child
        children_by_parent[parent].append(child)
        child_links.append(child)
    assert len(child_links) == len(set(child_links))
    assert set(child_links) == {
        link.get('name')
        for link in robot.findall('link')
        if link.get('name') != 'base_footprint'
    }

    reachable = set()
    pending = ['base_footprint']
    while pending:
        link = pending.pop()
        assert link not in reachable, 'joint graph contains a cycle'
        reachable.add(link)
        pending.extend(children_by_parent[link])
    assert reachable == links


def test_four_mecanum_wheels_keep_the_correct_corner_and_mesh():
    """ABAB wheel meshes, joint axes, and corner positions may not be swapped."""
    arguments, robot = _render_robot()
    expected = {
        'front_left': (
            'left_front_wheel_link.stl',
            arguments['wheel_x_offset'] + arguments['wheelbase'] / 2.0,
            arguments['wheel_y_offset'] + arguments['wheel_separation'] / 2.0,
            arguments['front_wheel_z'],
        ),
        'front_right': (
            'right_front_wheel_link.stl',
            arguments['wheel_x_offset'] + arguments['wheelbase'] / 2.0,
            arguments['wheel_y_offset'] - arguments['wheel_separation'] / 2.0,
            arguments['front_wheel_z'],
        ),
        'rear_left': (
            'left_back_wheel_link.stl',
            arguments['wheel_x_offset'] - arguments['wheelbase'] / 2.0,
            arguments['wheel_y_offset'] + arguments['wheel_separation'] / 2.0,
            arguments['rear_wheel_z'],
        ),
        'rear_right': (
            'right_back_wheel_link.stl',
            arguments['wheel_x_offset'] - arguments['wheelbase'] / 2.0,
            arguments['wheel_y_offset'] - arguments['wheel_separation'] / 2.0,
            arguments['rear_wheel_z'],
        ),
    }

    for name, (mesh_name, x, y, z) in expected.items():
        link_name = f'{name}_wheel_link'
        joint = _named(robot, 'joint', f'{name}_wheel_joint')
        assert joint.get('type') == 'continuous'
        assert _joint_links(joint) == ('base_link', link_name)
        assert _vector(joint.find('axis').get('xyz')) == (0.0, 1.0, 0.0)
        assert _vector(joint.find('origin').get('xyz')) == pytest.approx(
            (x, y, z)
        )

        link = _named(robot, 'link', link_name)
        visual_mesh = link.find('visual/geometry/mesh')
        assert visual_mesh is not None
        assert Path(visual_mesh.get('filename')).name == mesh_name
        sphere = link.find('collision/geometry/sphere')
        assert sphere is not None
        assert float(sphere.get('radius')) == pytest.approx(
            arguments['wheel_radius']
        )
        assert float(link.find('inertial/mass').get('value')) == pytest.approx(
            arguments['wheel_mass']
        )


def test_sensor_frames_have_the_profile_mounts_and_optical_convention():
    """Sensor topics depend on fixed frames being attached at the right pose."""
    arguments, robot = _render_robot()
    sensors = {
        'camera': ('camera_link', 'camera'),
        'laser': ('laser_frame', 'lidar'),
        'imu': ('imu_link', 'imu'),
        'microphone': ('microphone_link', 'microphone'),
    }
    for joint_prefix, (child, argument_prefix) in sensors.items():
        joint = _named(robot, 'joint', f'{joint_prefix}_joint')
        assert joint.get('type') == 'fixed'
        assert _joint_links(joint) == ('base_link', child)
        origin = joint.find('origin')
        assert origin is not None
        expected_xyz = tuple(
            arguments[f'{argument_prefix}_{axis}'] for axis in 'xyz'
        )
        assert _vector(origin.get('xyz')) == pytest.approx(expected_xyz)

    optical = _named(robot, 'joint', 'camera_depth_optical_joint')
    assert optical.get('type') == 'fixed'
    assert _joint_links(optical) == (
        'camera_link',
        'camera_depth_optical_frame',
    )
    assert _vector(optical.find('origin').get('xyz')) == (0.0, 0.0, 0.0)
    assert _vector(optical.find('origin').get('rpy')) == pytest.approx(
        (-math.pi / 2.0, 0.0, -math.pi / 2.0)
    )


def test_all_physical_links_have_positive_valid_inertia():
    """No physical component may be massless or have an invalid inertia."""
    _arguments, robot = _render_robot()
    virtual_links = {'base_footprint', 'camera_depth_optical_frame'}
    for link in robot.findall('link'):
        if link.get('name') in virtual_links:
            assert link.find('inertial') is None
            continue
        inertial = link.find('inertial')
        assert inertial is not None, link.get('name')
        mass = float(inertial.find('mass').get('value'))
        inertia = inertial.find('inertia')
        ixx, iyy, izz, ixy, ixz, iyz = (
            float(inertia.get(name))
            for name in ('ixx', 'iyy', 'izz', 'ixy', 'ixz', 'iyz')
        )
        diagonal = (ixx, iyy, izz)
        determinant = (
            ixx * (iyy * izz - iyz * iyz)
            - ixy * (ixy * izz - ixz * iyz)
            + ixz * (ixy * iyz - ixz * iyy)
        )
        assert math.isfinite(mass) and mass > 0.0
        assert all(
            math.isfinite(value)
            for value in (ixx, iyy, izz, ixy, ixz, iyz)
        )
        assert ixx > 0.0
        assert ixx * iyy - ixy * ixy > 0.0
        assert determinant > 0.0
        assert diagonal[0] <= diagonal[1] + diagonal[2]
        assert diagonal[1] <= diagonal[0] + diagonal[2]
        assert diagonal[2] <= diagonal[0] + diagonal[1]


def test_referenced_meshes_exist_and_use_package_portable_paths():
    """Every generated mesh URI must resolve without a user-specific path."""
    _arguments, robot = _render_robot()
    referenced = set()
    for mesh in robot.findall('.//mesh'):
        filename = mesh.get('filename')
        assert filename.startswith('file://')
        path = Path(filename.removeprefix('file://'))
        assert path.is_file(), path
        referenced.add(path.name)

    assert {
        'base_link.stl',
        'base_link_collision.stl',
        'depth_camera_link.stl',
        'depth_camera_link_collision.stl',
        'lidar_link.stl',
        'lidar_link_collision.stl',
        'mic_link.stl',
        'mic_link_collision.stl',
        'left_front_wheel_link.stl',
        'right_front_wheel_link.stl',
        'left_back_wheel_link.stl',
        'right_back_wheel_link.stl',
    } == referenced


def test_official_mesh_inventory_and_checksums_are_unchanged():
    """Vendored Hiwonder meshes must remain byte-identical to the import."""
    mesh_root = PACKAGE_ROOT / 'meshes'
    actual = {
        path.name
        for path in mesh_root.iterdir()
        if path.is_file() and path.suffix.lower() == '.stl'
    }
    assert actual == OFFICIAL_MESHES
    assert (mesh_root / 'SOURCE.md').is_file()

    expected = {}
    for line in (mesh_root / 'SHA256SUMS').read_text(
        encoding='utf-8'
    ).splitlines():
        digest, filename = line.split(maxsplit=1)
        expected[filename] = digest
    assert set(expected) == OFFICIAL_MESHES
    for filename, digest in expected.items():
        assert hashlib.sha256(
            (mesh_root / filename).read_bytes()
        ).hexdigest() == digest
