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
    assert config['debug_image_transport'] == 'compressed'
    assert config['compressed_debug_image_topic'].endswith('/compressed')
    assert 1 <= config['debug_jpeg_quality'] <= 100
    assert config['max_inference_rate_hz'] == 12.0
    assert config['inference_backend'] == 'auto'
    assert config['dnn_target'] == 'auto'
    assert config['opencv_num_threads'] == 4
    assert config['reid_backend'] == 'auto'
    assert config['reid_refresh_interval_frames'] == 3
    assert config['reid_max_inactive_frames'] >= 2400
    assert config['projection_frame'] == ''
    assert config['use_sim_time'] is False


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
    script = PACKAGE_ROOT / 'scripts' / 'prepare_yolo26_model.sh'
    source = script.read_text(encoding='utf-8')
    assert script.stat().st_mode & 0o111
    assert 'ultralytics==8.4.55' in source
    assert '9b09cc8bf347f0fc8a5f7657480587f25db09b34bf33b0652110fb03a8ad4fef' in source
    assert "'torch==2.5.1'" in source
    assert "'onnx==1.20.1'" in source
    assert 'end2end=True' in source
    assert '/home/' not in source
    assert '/Users/' not in source

    runtime_script = (
        PACKAGE_ROOT / 'scripts' / 'prepare_inference_runtime.sh'
    )
    runtime_source = runtime_script.read_text(encoding='utf-8')
    assert runtime_script.stat().st_mode & 0o111
    assert 'onnxruntime_gpu-1.23.0-cp310-cp310-linux_aarch64.whl' in runtime_source
    assert "'numpy==1.23.5'" in runtime_source
    assert '/home/' not in runtime_source
    assert '/Users/' not in runtime_source

    reid_script = PACKAGE_ROOT / 'scripts' / 'prepare_osnet_model.sh'
    reid_source = reid_script.read_text(encoding='utf-8')
    assert reid_script.stat().st_mode & 0o111
    assert 'f8cd150fdf77e8d9e1ed143b7f308c2c609ded50' in reid_source
    assert '8a07e8da38946f7cee37f4561617bf8b6d2fe8f3a4027852893ea092e46d919f' in reid_source
    assert 'osnet_ain_x1_0' in reid_source
    assert '/home/' not in reid_source
    assert '/Users/' not in reid_source
