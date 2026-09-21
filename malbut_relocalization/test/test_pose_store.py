"""Validate persistent localization records without starting ROS."""

from malbut_relocalization.pose_store import (
    map_identity, read_initial_pose, read_pose, write_pose,
)
import pytest


def _pose():
    return {'frame_id': 'map', 'x': 1.0, 'y': 2.0, 'yaw': 0.3,
            'covariance': [0.1 if i % 7 == 0 else 0.0 for i in range(36)]}


def test_pose_matches_map_contents_not_just_filename(tmp_path):
    """Replacing an image at the same path must invalidate the old pose."""
    image = tmp_path / 'home.pgm'
    image.write_bytes(b'P5\n1 1\n255\n\xff')
    source = tmp_path / 'home.yaml'
    source.write_text('image: home.pgm\nresolution: 0.05\n')
    identity = map_identity(source)
    path = tmp_path / 'localization/last_pose.yaml'
    write_pose(path, identity, _pose())
    saved = read_pose(path, identity)
    assert saved['x'] == 1.0
    assert saved['covariance'] == _pose()['covariance']
    assert 'saved_at' in saved
    image.write_bytes(b'P5\n1 1\n255\n\x00')
    assert read_pose(path, map_identity(source)) is None
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize('update', [
    {'x': float('nan')}, {'yaw': float('inf')}, {'y': 'bad'},
    {'covariance': [0] * 35}, {'covariance': [-1] * 36}, {'frame_id': 'odom'},
])
def test_invalid_pose_never_overwrites_existing_record(tmp_path, update):
    """Bad localization data cannot destroy the previous usable record."""
    path = tmp_path / 'pose.yaml'
    write_pose(path, 'map', _pose())
    before = path.read_bytes()
    with pytest.raises(ValueError):
        write_pose(path, 'map', {**_pose(), **update})
    assert path.read_bytes() == before


def test_invalid_and_missing_files_are_ignored(tmp_path):
    """A corrupt record falls back to manual initialization."""
    path = tmp_path / 'pose.yaml'
    assert read_pose(path, 'map') is None
    for value in ('[broken', 'null', '123', '{}'):
        path.write_text(value)
        assert read_pose(path, 'map') is None


def test_mapping_pose_fallback_matches_map_and_preserves_amcl_memory(tmp_path):
    """A newly mapped pose seeds startup, but matching AMCL memory takes precedence."""
    map_file = tmp_path / 'home.yaml'
    record = tmp_path / 'home.pose.yaml'
    amcl_file = tmp_path / 'last_pose.yaml'
    write_pose(record, 'saved-map', _pose())
    assert read_initial_pose(amcl_file, map_file, 'saved-map')['x'] == 1.0
    assert read_initial_pose(amcl_file, map_file, 'changed-map') is None
    write_pose(amcl_file, 'saved-map', {**_pose(), 'x': 3.0})
    assert read_initial_pose(amcl_file, map_file, 'saved-map')['x'] == 3.0
