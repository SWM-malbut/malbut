"""Check dominant-wall alignment, ambiguity and navigation-file preservation."""

import hashlib
import json
import math
from unittest.mock import Mock

import cv2
import numpy as np
import pytest
import yaml

from malbut_autoslam.map_view import estimate_view, read_map_view, write_map_view
from malbut_autoslam.saved_pose import write_mapping_pose


def _walls(angle=0, noise=False):
    image = np.zeros((620, 620), dtype=np.uint8)
    cv2.rectangle(image, (100, 170), (520, 450), 1, 4)
    cv2.line(image, (270, 170), (270, 310), 1, 4)
    if noise:
        rng = np.random.default_rng(123)
        for x, y in rng.integers(40, 580, (250, 2)):
            image[y, x] = 1
        cv2.line(image, (330, 240), (375, 290), 1, 3)
    matrix = cv2.getRotationMatrix2D((309.5, 309.5), angle, 1)
    return cv2.warpAffine(image, matrix, (620, 620), flags=cv2.INTER_NEAREST) != 0


@pytest.mark.parametrize('angle', [-38, -24, -13, -1, 0, 7, 21, 36, 44])
def test_rectangular_walls_and_clutter_align_to_nearest_axes(angle):
    result = estimate_view(_walls(angle, noise=True), 0.05)
    error = (result['rotation_rad'] - math.radians(angle) + math.pi / 4) % (
        math.pi / 2) - math.pi / 4
    # One-degree voting and pixel stair-stepping may retain a nearly level map.
    assert abs(math.degrees(error)) < 1.1
    assert result['confidence'] >= 0.65


@pytest.mark.parametrize('kind', ['empty', 'speckles', 'circle', 'competing'])
def test_insufficient_or_ambiguous_walls_are_not_forced_into_a_rectangle(kind):
    image = np.zeros((620, 620), dtype=np.uint8)
    if kind == 'speckles':
        rng = np.random.default_rng(7)
        for x, y in rng.integers(0, 620, (1500, 2)):
            image[y, x] = 1
    elif kind == 'circle':
        cv2.circle(image, (310, 310), 220, 1, 3)
    elif kind == 'competing':
        image = _walls(23) | _walls(-23)
    result = estimate_view(image != 0, 0.05)
    assert result['rotation_rad'] == 0.0


@pytest.fixture
def saved(tmp_path):
    """Write a trinary ROS map, without requiring any ROS process."""
    path = tmp_path / 'home.yaml'
    pixels = np.where(_walls(21), 0, 254).astype(np.uint8)
    cv2.imwrite(str(path.with_suffix('.pgm')), pixels)
    path.write_text(yaml.safe_dump({
        'image': 'home.pgm', 'resolution': 0.05, 'origin': [-3.0, -4.0, 0.2],
        'mode': 'trinary', 'negate': 0, 'occupied_thresh': 0.65, 'free_thresh': 0.196}))
    return path


def test_view_persistence_never_rewrites_navigation_map_or_exit_pose(saved):
    pose = write_mapping_pose(saved, 1.2, -0.4, 0.7)
    files = (saved, saved.with_suffix('.pgm'), pose)
    original = [path.read_bytes() for path in files]
    result = write_map_view(saved)
    assert [path.read_bytes() for path in files] == original
    assert read_map_view(saved) == result
    assert result['map_signature'] == hashlib.sha256(original[0] + original[1]).hexdigest()
    assert abs(math.degrees(result['rotation_rad']) - 21) < 1


def test_stale_map_or_damaged_view_is_ignored(saved):
    assert read_map_view(saved) is None
    write_map_view(saved)
    saved.with_suffix('.view.json').write_text('{broken')
    assert read_map_view(saved) is None
    write_map_view(saved)
    saved.write_text(saved.read_text() + '\n# different map metadata\n')
    assert read_map_view(saved) is None


def test_unknown_cells_are_not_used_as_a_wall(saved):
    cv2.imwrite(str(saved.with_suffix('.pgm')), np.full((50, 50), 205, np.uint8))
    result = write_map_view(saved)
    assert result['rotation_rad'] == 0.0 and result['confidence'] == 0.0


def test_negated_occupancy_keeps_the_same_wall_direction(saved):
    metadata = yaml.safe_load(saved.read_text())
    metadata['negate'] = 1
    saved.write_text(yaml.safe_dump(metadata))
    cv2.imwrite(str(saved.with_suffix('.pgm')), np.where(_walls(21), 255, 0).astype(np.uint8))
    assert abs(math.degrees(write_map_view(saved)['rotation_rad']) - 21) < 1


def test_atomic_write_failure_keeps_previous_view_and_removes_temporary(saved, monkeypatch):
    write_map_view(saved)
    destination = saved.with_suffix('.view.json')
    before = destination.read_bytes()
    monkeypatch.setattr('malbut_autoslam.map_view.os.replace', Mock(side_effect=OSError('full')))
    with pytest.raises(OSError):
        write_map_view(saved)
    assert destination.read_bytes() == before
    assert not list(saved.parent.glob('.map-view-*'))


def test_nonfinite_rotation_cannot_enter_a_preview(saved):
    view = write_map_view(saved)
    view['rotation_rad'] = float('nan')
    saved.with_suffix('.view.json').write_text(json.dumps(view))
    assert read_map_view(saved) is None
