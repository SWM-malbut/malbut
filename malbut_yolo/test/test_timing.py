"""Unit-test the diagnostic wrapper, not the upstream inference algorithm."""

import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

from malbut_interfaces.msg import SensorProcessingTrace


@pytest.fixture
def wrapper(monkeypatch):
    """Import only our wrapper with a deliberately minimal upstream stand-in."""
    upstream = ModuleType('yolo_ros.yolo_node')

    class StubYoloNode:
        """Record forwarding without importing Torch or loading any weights."""

        def image_cb(self, message):
            """Stand in for one successful upstream inference callback."""
            self.seen.append(message)
            if self.failure:
                raise RuntimeError('inference failed')

    upstream.YoloNode = StubYoloNode
    monkeypatch.setitem(sys.modules, 'yolo_ros.yolo_node', upstream)
    source = Path(__file__).parents[1] / 'malbut_yolo/yolo_node.py'
    spec = importlib.util.spec_from_file_location('tested_yolo_wrapper', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _instance(module, enabled=True):
    node = module.TimedYoloNode.__new__(module.TimedYoloNode)
    node.enable = enabled
    node.seen = []
    node.failure = False
    node._timing_sequence = 0
    node.traces = []
    node._timing_pub = SimpleNamespace(publish=node.traces.append)
    return node


def _image():
    stamp = SensorProcessingTrace().source_stamp
    stamp.sec = 42
    stamp.nanosec = 123456789
    return SimpleNamespace(header=SimpleNamespace(stamp=stamp))


def test_trace_keeps_sensor_stamp_and_uses_monotonic_callback_boundaries(wrapper, monkeypatch):
    """ROS timestamps identify images; processing duration uses one real clock."""
    node = _instance(wrapper)
    calls = []
    clock = iter([1_000, 9_000, 10_000, 15_000])

    def monotonic(clock_id):
        calls.append(clock_id)
        return next(clock)

    monkeypatch.setattr(wrapper.time, 'clock_gettime_ns', monotonic)
    first = _image()
    second = _image()
    second.header.stamp.sec += 1
    node.image_cb(first)
    node.image_cb(second)
    assert node.seen == [first, second]
    assert calls == [wrapper.time.CLOCK_MONOTONIC] * 4
    assert [item.sequence for item in node.traces] == [1, 2]
    assert node.traces[0].source_stamp == first.header.stamp
    assert node.traces[1].source_stamp == second.header.stamp
    assert node.traces[0].receipt_steady_time_ns == 1_000
    assert node.traces[0].publish_steady_time_ns == 9_000
    assert node.traces[1].receipt_steady_time_ns == 10_000
    assert node.traces[1].publish_steady_time_ns == 15_000
    assert all(item.source == 'camera' for item in node.traces)


def test_disabled_detection_does_not_publish_fake_timing(wrapper):
    """A disabled callback is not counted as an inference sample."""
    node = _instance(wrapper, enabled=False)
    node.image_cb(_image())
    assert not node.seen
    assert not node.traces
    assert node._timing_sequence == 0


def test_failed_inference_does_not_publish_completed_trace(wrapper):
    """A callback exception cannot appear as a successful output sample."""
    node = _instance(wrapper)
    node.failure = True
    with pytest.raises(RuntimeError, match='inference failed'):
        node.image_cb(_image())
    assert not node.traces
    assert node._timing_sequence == 0
