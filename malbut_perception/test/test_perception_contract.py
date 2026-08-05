"""Package contract tests for sensor-only person perception."""

import ast
from pathlib import Path

import yaml


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def test_config_uses_only_rgbd_inputs_and_standard_outputs():
    config = yaml.safe_load(
        (PACKAGE_ROOT / 'config' / 'person_detection.yaml').read_text(
            encoding='utf-8'
        )
    )['person_localizer']['ros__parameters']
    assert config['rgb_topic'] == '/camera/color/image_raw'
    assert config['depth_topic'] == '/camera/depth/image_raw'
    assert config['camera_info_topic'] == '/camera/color/camera_info'
    assert config['detections_2d_topic'].endswith('/detections_2d')
    assert config['detections_3d_topic'].endswith('/detections_3d')


def test_node_contains_no_simulator_ground_truth_subscription():
    node_source = (
        PACKAGE_ROOT / 'malbut_perception' / 'target_localizer_node.py'
    ).read_text(encoding='utf-8')
    lowered = node_source.lower()
    forbidden = (
        'humanoid_target',
        '/model/',
        'actor_name',
        'pose/info',
        'gazebo_msgs',
        'ros_gz_interfaces',
    )
    assert all(token not in lowered for token in forbidden)


def test_launch_file_is_valid_python_and_installed():
    launch_file = PACKAGE_ROOT / 'launch' / 'person_detection.launch.py'
    ast.parse(launch_file.read_text(encoding='utf-8'))
    setup_source = (PACKAGE_ROOT / 'setup.py').read_text(encoding='utf-8')
    assert "glob('launch/*.launch.py')" in setup_source
    assert "glob('config/*.yaml')" in setup_source


def test_model_preparation_is_pinned_and_machine_independent():
    script = PACKAGE_ROOT / 'scripts' / 'prepare_yolov5_model.sh'
    source = script.read_text(encoding='utf-8')
    assert script.stat().st_mode & 0o111
    assert '915bbf294bb74c859f0b41f1c23bc395014ea679' in source
    assert "'torch==2.5.1'" in source
    assert "'onnx==1.16.2'" in source
    assert '/home/' not in source
    assert '/Users/' not in source
