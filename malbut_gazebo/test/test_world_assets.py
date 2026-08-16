"""Validation tests for Fortress worlds and vendored household assets."""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
from xml.etree import ElementTree

from malbut_gazebo.world_catalog import load_world_catalog


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
WORLDS_DIRECTORY = PACKAGE_ROOT / 'worlds'
AWS_MODELS = PACKAGE_ROOT / 'models' / 'aws_small_house'
CATALOG_FILE = PACKAGE_ROOT / 'config' / 'worlds.yaml'
REQUIRED_SYSTEMS = {
    'ignition-gazebo-physics-system',
    'ignition-gazebo-user-commands-system',
    'ignition-gazebo-scene-broadcaster-system',
    'ignition-gazebo-sensors-system',
    'ignition-gazebo-imu-system',
}


def _world_files():
    catalog = load_world_catalog(CATALOG_FILE)
    return [WORLDS_DIRECTORY / entry['file'] for entry in catalog.values()]


def test_catalogued_worlds_are_portable_and_named_consistently():
    """Every catalog entry must resolve to one portable Fortress SDF world."""
    catalog = load_world_catalog(CATALOG_FILE)
    assert {path.name for path in _world_files()} == {
        path.name for path in WORLDS_DIRECTORY.glob('*.sdf')
    }
    for name, entry in catalog.items():
        world_file = WORLDS_DIRECTORY / entry['file']
        text = world_file.read_text(encoding='utf-8')
        root = ElementTree.fromstring(text)
        world = root.find('world')
        assert root.tag == 'sdf'
        assert world is not None
        assert world.get('name') == name
        assert '/home/' not in text
        assert '/Users/' not in text
        assert 'libgazebo_' not in text
        assert 'gazebo_ros' not in text


def test_all_worlds_pass_libsdformat_validation():
    """XML parsing alone is insufficient; libsdformat must accept each world."""
    ignition = shutil.which('ign')
    assert ignition is not None
    environment = os.environ.copy()
    current_sdf_path = environment.get('SDF_PATH', '')
    environment['SDF_PATH'] = os.pathsep.join(
        path
        for path in (str(AWS_MODELS), current_sdf_path)
        if path
    )
    for world_file in _world_files():
        result = subprocess.run(
            [ignition, 'sdf', '-k', str(world_file)],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=30,
        )
        assert result.returncode == 0, (
            f'{world_file.name}: {result.stdout}\n{result.stderr}'
        )


def test_each_world_loads_required_fortress_systems_once():
    """Physics, commands, rendering, LiDAR/camera, and IMU must be available."""
    for world_file in _world_files():
        world = ElementTree.parse(world_file).getroot().find('world')
        plugins = [plugin.get('filename') for plugin in world.findall('plugin')]
        assert all(plugins)
        assert len(plugins) == len(set(plugins))
        assert REQUIRED_SYSTEMS <= set(plugins), world_file.name
        physics = world.find('physics')
        assert physics is not None
        assert physics.get('type') == 'ignored'
        assert float(physics.findtext('max_step_size')) > 0.0
        assert float(physics.findtext('real_time_factor')) > 0.0


def test_small_house_model_references_resolve_to_the_vendored_subset():
    """All model URIs must resolve locally and every copied model must be used."""
    world = ElementTree.parse(
        WORLDS_DIRECTORY / 'small_house.sdf'
    ).getroot().find('world')
    direct_includes = world.findall('include')
    assert direct_includes
    assert not world.findall('model/include')
    instance_names = [include.findtext('name') for include in direct_includes]
    assert all(instance_names)
    assert len(instance_names) == len(set(instance_names))

    references = {
        include.findtext('uri').removeprefix('model://')
        for include in direct_includes
    }
    model_directories = {
        path.name for path in AWS_MODELS.iterdir() if path.is_dir()
    }
    assert references == model_directories
    assert not any('Portrait' in name for name in references)


def test_vendored_models_resolve_meshes_textures_and_collision_geometry():
    """A visible household object may not be missing assets or collision."""
    for model_directory in sorted(
        path for path in AWS_MODELS.iterdir() if path.is_dir()
    ):
        model_file = model_directory / 'model.sdf'
        assert model_file.is_file()
        assert (model_directory / 'model.config').is_file()
        root = ElementTree.parse(model_file).getroot()
        collisions = root.findall('.//collision')
        assert collisions, model_directory.name
        assert all(collision.find('geometry') is not None for collision in collisions)

        for uri in root.findall('.//mesh/uri'):
            prefix = f'model://{model_directory.name}/'
            assert uri.text is not None
            assert uri.text.startswith(prefix)
            assert (model_directory / uri.text.removeprefix(prefix)).is_file()

        referenced_textures = set()
        for mesh_file in (model_directory / 'meshes').glob('*.DAE'):
            mesh_root = ElementTree.parse(mesh_file).getroot()
            for source in mesh_root.findall('.//{*}init_from'):
                if source.text and source.text.lower().endswith('.png'):
                    texture = (mesh_file.parent / source.text).resolve()
                    assert texture.is_relative_to(model_directory.resolve())
                    assert texture.is_file()
                    referenced_textures.add(texture)
        bundled_textures = set(
            (model_directory / 'materials' / 'textures').glob('*.png')
        )
        assert referenced_textures == bundled_textures


def test_imported_maps_and_third_party_records_keep_their_provenance():
    """Static maps and third-party source records must remain auditable."""
    expected = {
        'robocup_home.pgm': (
            '0f6e74f0c9fd732807b3fd10207309369'
            'ac272d184bac17932c1be0b52e3593e'
        ),
        'small_house.pgm': (
            '4406c72e26c2ef743c8976406495bebc'
            '975327f3e322b57c8344f9076a2fe41c'
        ),
    }
    for filename, digest in expected.items():
        actual = hashlib.sha256(
            (PACKAGE_ROOT / 'maps' / filename).read_bytes()
        ).hexdigest()
        assert actual == digest

    assert (AWS_MODELS / 'LICENSE').is_file()
    source_record = (AWS_MODELS / 'SOURCE.md').read_text(encoding='utf-8')
    assert 'ff9631ca6d1db9c1ba656498151464b5ab74aafe' in source_record
    paths = [str(path.relative_to(AWS_MODELS)) for path in AWS_MODELS.rglob('*')]
    assert not any('Portrait' in path for path in paths)
    assert not any('/photos/' in f'/{path}/' for path in paths)
