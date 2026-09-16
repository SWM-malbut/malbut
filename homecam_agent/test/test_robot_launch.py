"""Check real-robot media wiring without drivers, models, or cloud requests."""

import importlib.util
from pathlib import Path

from launch import LaunchContext
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.utilities import normalize_to_list_of_substitutions, perform_substitutions
from launch_ros.actions import Node
from launch_ros.utilities import evaluate_parameters


ROOT = Path(__file__).parents[1]


def _launch(name, **overrides):
    source = ROOT / "homecam_media_agent/launch" / name
    spec = importlib.util.spec_from_file_location(name.replace(".", "_"), source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    description = module.generate_launch_description()
    context = LaunchContext()
    context.launch_configurations.update(overrides)
    for action in description.entities:
        if isinstance(action, DeclareLaunchArgument):
            action.execute(context)
    return description.entities, context


def test_robot_reuses_media_with_real_camera_and_no_duplicate_detector():
    actions, context = _launch("homecam_robot.launch.py", device_id="registered-robot")
    include = next(action for action in actions if isinstance(action, IncludeLaunchDescription))
    options = {
        name: perform_substitutions(context, normalize_to_list_of_substitutions(value))
        for name, value in include.launch_arguments
    }
    assert options["start_detector"] == "false"
    children, child_context = _launch("homecam_aurora.launch.py", **options)
    active = [action for action in children if isinstance(action, Node)
              and (action.condition is None or action.condition.evaluate(child_context))]
    assert [node.node_package for node in active] == ["homecam_media_agent"]
    parameters = {}
    for values in evaluate_parameters(child_context, active[0]._Node__parameters):
        if isinstance(values, dict):
            parameters.update(values)
    assert parameters["image_topic"] == "/depth_cam/rgb0/image_raw"
    assert parameters["camera_info_topic"] == "/depth_cam/rgb0/camera_info"
    assert parameters["odom_topic"] == "/odom"
    assert parameters["device_id"] == "registered-robot"
    assert parameters["use_sim_time"] is False


def test_standalone_aurora_keeps_optional_detector_compatibility():
    actions, context = _launch("homecam_aurora.launch.py", image_topic="/custom/rgb")
    detector = next(action for action in actions
                    if isinstance(action, Node) and action.node_package == "homecam_detector")
    assert detector.condition.evaluate(context)
    context.launch_configurations["start_detector"] = "false"
    assert not detector.condition.evaluate(context)


def test_robot_service_does_not_inject_a_second_inference_runtime():
    service = (ROOT / "systemd/malbut-homecam.service.in").read_text()
    assert "homecam_robot.launch.py" in service
    assert "model_path:=" not in service
    assert "ONNX_RUNTIME" not in service
    assert "PYTHONPATH" not in service
    assert "HOMECAM_DEVICE_TOKEN" not in service
    assert 'source "@OVERLAY_SETUP@"' in service
    installer = (ROOT / "scripts/install_homecam_systemd.sh").read_text()
    assert '--overlay) overlay="${2:-}"' in installer
    assert 'overlay_setup="$workspace/install/malbut_test/local_setup.bash"' in installer
    assert 'overlay_setup="$workspace/install/local_setup.bash"' in installer
    assert 's|@OVERLAY_SETUP@|$overlay_setup|g' in installer
