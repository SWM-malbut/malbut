"""Tests for the simulation world catalog."""

import hashlib
from pathlib import Path
from xml.etree import ElementTree

import pytest

from malbut_gazebo.world_catalog import load_world_catalog, resolve_world


PACKAGE_ROOT = Path(__file__).parents[1]
CATALOG_FILE = PACKAGE_ROOT / 'config' / 'worlds.yaml'
WORLDS_DIRECTORY = PACKAGE_ROOT / 'worlds'


def test_catalog_contains_the_supported_world_set():
    catalog = load_world_catalog(CATALOG_FILE)
    assert set(catalog) == {
        'empty',
        'test_arena',
        'robocup_home',
        'small_house',
    }


@pytest.mark.parametrize(
    'world_name',
    ['empty', 'test_arena', 'robocup_home', 'small_house'],
)
def test_catalog_world_exists_and_has_matching_sdf_name(world_name):
    world_file, _ = resolve_world(
        CATALOG_FILE,
        WORLDS_DIRECTORY,
        world_name,
    )
    root = ElementTree.parse(world_file).getroot()
    world = root.find('world')
    assert world is not None
    assert world.get('name') == world_name


def test_small_house_has_a_known_upstream_free_space_spawn():
    catalog = load_world_catalog(CATALOG_FILE)
    spawn = catalog['small_house']['spawn']
    assert spawn == {
        'x': -3.665503,
        'y': -0.4874,
        'z': 0.002,
        'yaw': 0.0,
    }


def test_robocup_home_map_is_named_for_its_matching_world():
    map_yaml = PACKAGE_ROOT / 'maps' / 'robocup_home.yaml'
    map_image = PACKAGE_ROOT / 'maps' / 'robocup_home.pgm'
    assert map_yaml.is_file()
    assert 'image: robocup_home.pgm' in map_yaml.read_text(encoding='utf-8')
    assert map_image.is_file()
    assert hashlib.sha256(map_image.read_bytes()).hexdigest() == (
        '0f6e74f0c9fd732807b3fd10207309369'
        'ac272d184bac17932c1be0b52e3593e'
    )


def test_unknown_world_is_rejected():
    with pytest.raises(RuntimeError, match='Unknown world_name'):
        resolve_world(CATALOG_FILE, WORLDS_DIRECTORY, '../outside')
