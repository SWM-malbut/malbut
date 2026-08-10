"""Behavioral tests for world-catalog validation and spawn safety."""

from copy import deepcopy
import math
from pathlib import Path
from xml.etree import ElementTree

import cv2
import pytest
import yaml

from malbut_gazebo.world_catalog import load_world_catalog, resolve_world


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CATALOG_FILE = PACKAGE_ROOT / 'config' / 'worlds.yaml'
WORLDS_DIRECTORY = PACKAGE_ROOT / 'worlds'
MAPS_DIRECTORY = PACKAGE_ROOT / 'maps'


def _catalog_document():
    return yaml.safe_load(CATALOG_FILE.read_text(encoding='utf-8'))


def _write_catalog(tmp_path, document):
    path = tmp_path / 'worlds.yaml'
    path.write_text(
        yaml.safe_dump(document, sort_keys=False),
        encoding='utf-8',
    )
    return path


def test_every_catalog_entry_resolves_to_the_matching_world():
    """Catalog names, filenames, and SDF world names must agree."""
    catalog = load_world_catalog(CATALOG_FILE)
    assert set(catalog) == {
        'empty',
        'test_arena',
        'robocup_home',
        'small_house',
    }
    for name, expected in catalog.items():
        world_file, resolved = resolve_world(
            CATALOG_FILE,
            WORLDS_DIRECTORY,
            name,
        )
        assert resolved == expected
        world = ElementTree.parse(world_file).getroot().find('world')
        assert world is not None
        assert world.get('name') == name


@pytest.mark.parametrize(
    ('mutation', 'message'),
    [
        (
            lambda document: document.__setitem__('schema_version', 2),
            'Unsupported or missing',
        ),
        (
            lambda document: document['worlds'].__setitem__(
                '../escape', deepcopy(document['worlds']['empty'])
            ),
            'Invalid world name',
        ),
        (
            lambda document: document['worlds']['empty'].__setitem__(
                'file', '../empty.sdf'
            ),
            'Invalid world filename',
        ),
        (
            lambda document: document['worlds']['empty']['spawn'].pop('yaw'),
            'must define spawn keys',
        ),
        (
            lambda document: document['worlds']['empty']['spawn'].__setitem__(
                'x', True
            ),
            'spawn.x is not numeric',
        ),
        (
            lambda document: document['worlds']['empty']['spawn'].__setitem__(
                'x', float('nan')
            ),
            'spawn.x is not finite',
        ),
    ],
)
def test_invalid_catalog_data_is_rejected(tmp_path, mutation, message):
    """Invalid names, paths, and poses must fail before launch execution."""
    document = _catalog_document()
    mutation(document)
    with pytest.raises(RuntimeError, match=message):
        load_world_catalog(_write_catalog(tmp_path, document))


def test_unknown_or_missing_world_is_rejected(tmp_path):
    """World lookup may not escape the catalog or return a missing SDF."""
    with pytest.raises(RuntimeError, match='Unknown world_name'):
        resolve_world(CATALOG_FILE, WORLDS_DIRECTORY, '../outside')

    document = _catalog_document()
    document['worlds']['empty']['file'] = 'missing.sdf'
    catalog = _write_catalog(tmp_path, document)
    with pytest.raises(RuntimeError, match='does not exist'):
        resolve_world(catalog, WORLDS_DIRECTORY, 'empty')


@pytest.mark.parametrize('world_name', ['robocup_home', 'small_house'])
def test_navigation_world_spawn_has_robot_sized_free_map_clearance(world_name):
    """The default spawn must be free in the map used by Nav2."""
    catalog = load_world_catalog(CATALOG_FILE)
    spawn = catalog[world_name]['spawn']
    metadata = yaml.safe_load(
        (MAPS_DIRECTORY / f'{world_name}.yaml').read_text(encoding='utf-8')
    )
    image = cv2.imread(
        str(MAPS_DIRECTORY / metadata['image']),
        cv2.IMREAD_GRAYSCALE,
    )
    assert image is not None

    origin_x, origin_y, origin_yaw = metadata['origin']
    delta_x = spawn['x'] - origin_x
    delta_y = spawn['y'] - origin_y
    cosine = math.cos(-origin_yaw)
    sine = math.sin(-origin_yaw)
    local_x = cosine * delta_x - sine * delta_y
    local_y = sine * delta_x + cosine * delta_y
    resolution = float(metadata['resolution'])
    cell_x = math.floor(local_x / resolution)
    cell_y = math.floor(local_y / resolution)
    row = image.shape[0] - 1 - cell_y
    assert 0 <= cell_x < image.shape[1]
    assert 0 <= row < image.shape[0]

    robot_clearance = 0.25
    radius_cells = math.ceil(robot_clearance / resolution)
    occupied_threshold = int(
        round(255 * (1.0 - float(metadata['free_thresh'])))
    )
    pixels = []
    for offset_y in range(-radius_cells, radius_cells + 1):
        for offset_x in range(-radius_cells, radius_cells + 1):
            if math.hypot(offset_x, offset_y) * resolution > robot_clearance:
                continue
            sample_row = row - offset_y
            sample_column = cell_x + offset_x
            assert 0 <= sample_row < image.shape[0]
            assert 0 <= sample_column < image.shape[1]
            pixels.append(image[sample_row, sample_column])
    assert pixels
    assert all(int(pixel) >= occupied_threshold for pixel in pixels)
