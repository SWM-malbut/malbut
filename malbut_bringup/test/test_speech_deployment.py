"""Check that the robot copy can install its complete speech runtime."""

import ast
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest


SOURCE = Path(__file__).parents[2]
ROBOT = SOURCE / 'malbut_test'
SPEECH_PACKAGES = ('malbut_agent_server', 'malbut_stt', 'malbut_tts')


@pytest.mark.parametrize('package', SPEECH_PACKAGES)
def test_robot_speech_runtime_matches_source(package):
    """Preserve executable modules and bundled Agent data without stale copies."""
    source = SOURCE / package / package
    deployed = ROBOT / package / package
    suffixes = {'.py', '.json', '.jsonl'}
    files = {path.relative_to(source) for path in source.rglob('*')
             if path.is_file() and path.suffix in suffixes}
    copied = {path.relative_to(deployed) for path in deployed.rglob('*')
              if path.is_file() and path.suffix in suffixes}
    assert files == copied
    for path in files:
        assert (source / path).read_bytes() == (deployed / path).read_bytes(), path


def test_robot_speech_launch_and_native_assets_match_source():
    """Ship the launch gate, preflight modules and Jetson model bridge together."""
    for path in (
        'malbut_bringup/launch/robot.launch.py',
        'malbut_bringup/launch/speech.launch.py',
        'malbut_bringup/malbut_bringup/speech_preflight.py',
        'malbut_bringup/malbut_bringup/speech_process.py',
        'malbut_stt/malbut_stt/preflight.py',
        'malbut_stt/config/jetson.yaml',
        'malbut_stt/native/CMakeLists.txt',
        'malbut_stt/native/whisper_cpp_bridge.cpp',
        'malbut_stt/requirements-whisper-cpp.txt',
        'malbut_tts/requirements-api.txt',
    ):
        assert (SOURCE / path).read_bytes() == (ROBOT / path).read_bytes(), path


@pytest.mark.parametrize('package', SPEECH_PACKAGES)
def test_robot_speech_install_metadata_is_self_contained(monkeypatch, package):
    """Resolve installed assets and console targets using only the robot folder."""
    metadata = {}
    setuptools = SimpleNamespace(
        find_packages=lambda **kwargs: [],
        setup=lambda **kwargs: metadata.update(kwargs),
    )
    monkeypatch.setitem(sys.modules, 'setuptools', setuptools)
    root = ROBOT / package
    monkeypatch.chdir(root)
    runpy.run_path(str(root / 'setup.py'))
    for _, paths in metadata['data_files']:
        assert paths
        for path in paths:
            assert (root / path).is_file(), path
    for name, patterns in metadata.get('package_data', {}).items():
        for pattern in patterns:
            assert list((root / name).glob(pattern)), pattern
    entries = metadata['entry_points']['console_scripts']
    for entry in entries:
        _, target = entry.split('=', 1)
        module, function = target.strip().split(':', 1)
        path = root / (module.replace('.', '/') + '.py')
        tree = ast.parse(path.read_text())
        assert any(isinstance(node, ast.FunctionDef) and node.name == function
                   for node in tree.body), target
    source_metadata = {}
    monkeypatch.setattr(setuptools, 'setup', lambda **kwargs: source_metadata.update(kwargs))
    monkeypatch.chdir(SOURCE / package)
    runpy.run_path(str(SOURCE / package / 'setup.py'))
    assert entries == source_metadata['entry_points']['console_scripts']


def test_robot_bringup_installs_speech_with_cmake():
    """The native Bringup package installs its Python speech entrypoint too."""
    for base in (SOURCE, ROBOT):
        root = base / 'malbut_bringup'
        cmake = (root / 'CMakeLists.txt').read_text()
        assert 'ament_python_install_package(${PROJECT_NAME})' in cmake
        assert 'scripts/speech_preflight' in cmake
        assert 'README_SPEECH.md' in cmake
        assert 'install(DIRECTORY launch config' in cmake
        script = root / 'scripts/speech_preflight'
        assert script.stat().st_mode & 0o111
        tree = ast.parse(script.read_text())
        entry = next(node for node in tree.body if isinstance(node, ast.ImportFrom)
                     and node.module == 'malbut_bringup.speech_preflight')
        assert any(name.name == 'main' for name in entry.names)
        assert (root / 'launch/speech.launch.py').is_file()
        assert (root / 'README_SPEECH.md').is_file()
        assert not (root / 'setup.py').exists()
    for path in ('CMakeLists.txt', 'scripts/speech_preflight'):
        assert ((SOURCE / 'malbut_bringup' / path).read_bytes() ==
                (ROBOT / 'malbut_bringup' / path).read_bytes())


def test_robot_speech_interfaces_are_generated_from_current_definitions():
    """Generate every imported speech and weather type from this deployment copy."""
    interfaces = ROBOT / 'malbut_interfaces'
    cmake = (interfaces / 'CMakeLists.txt').read_text()
    required = set()
    for package in SPEECH_PACKAGES:
        for path in (ROBOT / package / package).rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if (isinstance(node, ast.ImportFrom) and node.module in
                        {'malbut_interfaces.msg', 'malbut_interfaces.srv',
                         'malbut_interfaces.action'}):
                    kind = node.module.rsplit('.', 1)[1]
                    required.update(f'{kind}/{name.name}.{kind}' for name in node.names)
    assert required
    for path in required:
        assert f'"{path}"' in cmake, path
        assert ((interfaces / path).read_bytes() ==
                (SOURCE / 'malbut_interfaces' / path).read_bytes()), path
    manifest = ElementTree.parse(ROBOT / 'malbut_bringup/package.xml')
    dependencies = {node.text for node in manifest.findall('exec_depend')}
    assert set(SPEECH_PACKAGES) <= dependencies


@pytest.mark.parametrize('package', SPEECH_PACKAGES)
def test_robot_speech_copy_excludes_tests_secrets_and_caches(package):
    """Keep developer tests, local credentials and generated caches off the robot."""
    forbidden = {'test', 'tests', '__pycache__', '.pytest_cache', '.env'}
    for path in (ROBOT / package).rglob('*'):
        assert path.name not in forbidden, path
        assert not (path.name.startswith('.env.') and path.name != '.env.example'), path
        assert path.suffix != '.pyc', path
