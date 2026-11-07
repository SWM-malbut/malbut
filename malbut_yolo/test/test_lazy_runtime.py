"""The inactive detector must not import Torch or Ultralytics."""

from pathlib import Path
import subprocess
import sys


def test_upstream_constructor_does_not_import_inference_runtime():
    """Exercise the real vendored class in a fresh process, without model files."""
    source = Path(__file__).parents[1] / 'vendor/yolo_ros/yolo_ros/yolo_ros/yolo_node.py'
    program = '''
import importlib.util
import sys
import rclpy
spec = importlib.util.spec_from_file_location('lazy_upstream', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
rclpy.init(args=[])
node = module.YoloNode()
try:
    assert 'torch' not in sys.modules
    assert 'ultralytics' not in sys.modules
    assert set(node.type_to_model) == {'YOLO', 'World', 'YOLOE'}
finally:
    node.destroy_node()
    rclpy.shutdown()
'''
    subprocess.run([sys.executable, '-c', program, str(source)], check=True, timeout=15)
