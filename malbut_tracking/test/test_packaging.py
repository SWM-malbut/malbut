"""Keep tracking-specific C++ and Python tools in one deployable package."""

import ast
from pathlib import Path
from xml.etree import ElementTree


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def test_lidar_preprocessing_is_built_inside_tracking():
    """Install scan preprocessing without a second ROS package dependency."""
    package = ElementTree.parse(PACKAGE_ROOT / 'package.xml').getroot()
    assert package.findtext('export/build_type') == 'ament_cmake'
    dependencies = {
        item.text for item in package if item.tag.endswith('depend')
    }
    assert 'ament_cmake_python' in dependencies
    assert 'laser_geometry' in dependencies
    assert 'malbut_lidar_preprocessor' not in dependencies

    cmake = (PACKAGE_ROOT / 'CMakeLists.txt').read_text(encoding='utf-8')
    assert 'ament_python_install_package(${PROJECT_NAME})' in cmake
    assert 'add_executable(lidar_foreground_preprocessor' in cmake
    assert (PACKAGE_ROOT / 'src/lidar_foreground_preprocessor.cpp').is_file()
    launch = (
        PACKAGE_ROOT / 'launch/lidar_foreground.launch.py'
    ).read_text(encoding='utf-8')
    assert "package='malbut_tracking'" in launch
    assert "executable='lidar_foreground_preprocessor'" in launch


def test_python_entrypoints_and_benchmark_assets_are_installed():
    """Keep existing ros2 run names and benchmark share paths available."""
    cmake = (PACKAGE_ROOT / 'CMakeLists.txt').read_text(encoding='utf-8')
    for name, module in (
        ('person_follower', 'person_follower_node'),
        ('person_localizer', 'person_localizer_node'),
        ('person_tracking_benchmark', 'benchmark.evaluator'),
    ):
        script = PACKAGE_ROOT / 'scripts' / name
        assert f'scripts/{name}' in cmake
        tree = ast.parse(script.read_text(encoding='utf-8'))
        assert any(
            isinstance(node, ast.ImportFrom)
            and node.module == f'malbut_tracking.{module}'
            and any(alias.name == 'main' for alias in node.names)
            for node in ast.walk(tree)
        )

    for directory in ('config', 'launch', 'actors'):
        assert f'malbut_tracking/benchmark/{directory}' in cmake
    assert 'DESTINATION share/${PROJECT_NAME}/benchmark' in cmake
