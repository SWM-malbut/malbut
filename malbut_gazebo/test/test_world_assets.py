"""Static checks for Fortress worlds and vendored model references."""

from pathlib import Path
from xml.etree import ElementTree


PACKAGE_ROOT = Path(__file__).parents[1]
WORLDS_DIRECTORY = PACKAGE_ROOT / 'worlds'
AWS_MODELS = PACKAGE_ROOT / 'models' / 'aws_small_house'


def _world_files():
    return sorted(WORLDS_DIRECTORY.glob('*.sdf'))


def test_all_world_files_are_portable_fortress_sdf():
    assert {path.name for path in _world_files()} == {
        'empty.sdf',
        'robocup_home.sdf',
        'small_house.sdf',
        'test_arena.sdf',
    }
    for world_file in _world_files():
        text = world_file.read_text(encoding='utf-8')
        root = ElementTree.fromstring(text)
        assert root.tag == 'sdf'
        assert root.find('world') is not None
        assert '/home/' not in text
        assert '/Users/' not in text
        assert 'libgazebo_' not in text
        assert 'gazebo_ros' not in text


def test_each_world_loads_each_system_plugin_once():
    for world_file in _world_files():
        world = ElementTree.parse(world_file).getroot().find('world')
        assert world is not None
        system_plugins = [
            plugin.get('name')
            for plugin in world.findall('plugin')
        ]
        assert all(system_plugins)
        assert len(system_plugins) == len(set(system_plugins))


def test_small_house_references_exactly_the_vendored_subset():
    root = ElementTree.parse(
        WORLDS_DIRECTORY / 'small_house.sdf'
    ).getroot()
    world = root.find('world')
    assert world is not None
    direct_includes = world.findall('include')
    assert len(direct_includes) == 67
    assert not world.findall('model/include')
    instance_names = [include.findtext('name') for include in direct_includes]
    assert all(instance_names)
    assert len(instance_names) == len(set(instance_names))
    references = {
        (uri.text or '').removeprefix('model://')
        for uri in world.findall('include/uri')
    }
    model_directories = {
        path.name
        for path in AWS_MODELS.iterdir()
        if path.is_dir()
    }
    assert len(references) == 43
    assert references == model_directories
    assert not any('Portrait' in name for name in references)


def test_vendored_model_files_and_asset_references_are_exact():
    for model_directory in sorted(
        path for path in AWS_MODELS.iterdir() if path.is_dir()
    ):
        model_file = model_directory / 'model.sdf'
        assert model_file.is_file()
        assert (model_directory / 'model.config').is_file()
        root = ElementTree.parse(model_file).getroot()
        for uri in root.findall('.//mesh/uri'):
            prefix = f'model://{model_directory.name}/'
            assert uri.text is not None
            assert uri.text.startswith(prefix)
            relative_path = uri.text.removeprefix(prefix)
            assert (model_directory / relative_path).is_file()

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


def test_aws_import_has_license_source_record_and_no_people_photos():
    assert (AWS_MODELS / 'LICENSE').is_file()
    assert (AWS_MODELS / 'SOURCE.md').is_file()
    paths = [str(path.relative_to(AWS_MODELS)) for path in AWS_MODELS.rglob('*')]
    assert not any('Portrait' in path for path in paths)
    assert not any('/photos/' in f'/{path}/' for path in paths)


def test_launch_files_do_not_use_gazebo_classic_or_old_create_flag():
    launch_text = '\n'.join(
        path.read_text(encoding='utf-8')
        for path in sorted((PACKAGE_ROOT / 'launch').rglob('*.py'))
    )
    assert 'gazebo_ros' not in launch_text
    assert 'libgazebo_' not in launch_text
    assert "'-entity'" not in launch_text
    assert '--headless-rendering' in launch_text


def test_robot_does_not_duplicate_world_sensor_systems():
    robot_xacro = (
        PACKAGE_ROOT / 'urdf' / 'robot.gazebo.xacro'
    ).read_text(encoding='utf-8')
    assert 'ignition-gazebo-sensors-system' not in robot_xacro
    assert 'ignition-gazebo-imu-system' not in robot_xacro
