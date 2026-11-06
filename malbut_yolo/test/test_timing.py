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
    node._processing_enabled = True
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


def test_idle_does_not_run_inference_even_if_vendor_enable_is_true(wrapper):
    node = _instance(wrapper)
    node._processing_enabled = False
    node.image_cb(_image())
    assert node.seen == node.traces == []


def test_failed_inference_does_not_publish_completed_trace(wrapper):
    """A callback exception cannot appear as a successful output sample."""
    node = _instance(wrapper)
    node.failure = True
    with pytest.raises(RuntimeError, match='inference failed'):
        node.image_cb(_image())
    assert not node.traces
    assert node._timing_sequence == 0


def _lifecycle_instance(wrapper):
    from unittest.mock import Mock

    node = _instance(wrapper)
    node._processing_enabled = False
    node._activated = True
    node._idle_since = None
    node._idle_timeout = 60.0
    node._idle_timer = Mock()
    node._sub = None
    node._classes = None
    node._set_classes_srv = None
    node.model_type, node.model, node.device = 'YOLO', 'cached.pt', 'cpu'
    node.fuse_model = True
    model = Mock()
    node.type_to_model = {'YOLO': Mock(return_value=model)}
    node.image_qos_profile = object()
    node.create_subscription = Mock(return_value=object())
    node.destroy_subscription = Mock()
    node.create_service = Mock(return_value=object())
    return node, model


def test_idle_has_no_weights_and_quick_resume_reuses_loaded_model(wrapper, monkeypatch):
    """Only first demand loads weights; short breaks retain the same instance."""
    node, model = _lifecycle_instance(wrapper)
    now = [0.0]
    monkeypatch.setattr(wrapper.time, 'monotonic', lambda: now[0])
    node._change_processing(False)
    node._expire_model()
    node.type_to_model['YOLO'].assert_not_called()
    node._change_processing(True)
    assert node.yolo is model
    model.to.assert_called_once_with('cpu')
    model.fuse.assert_called_once()
    node._change_processing(False)
    assert node._sub is None
    now[0] = 59.0
    node._expire_model()
    assert node.yolo is model
    node._change_processing(True)
    now[0] = 70.0
    node._expire_model()
    assert node.yolo is model
    node.type_to_model['YOLO'].assert_called_once()
    assert node.create_subscription.call_count == 2


def test_long_idle_releases_and_reload_preserves_explicit_classes(wrapper, monkeypatch):
    """Drop the last model reference only after grace period; reload on demand."""
    node, model = _lifecycle_instance(wrapper)
    now = [0.0]
    monkeypatch.setattr(wrapper.time, 'monotonic', lambda: now[0])
    node._change_processing(True)
    node.set_classes_cb(SimpleNamespace(classes=['person', 'box']), object())
    node._change_processing(False)
    now[0] = 61.0
    node._expire_model()
    assert not hasattr(node, 'yolo')
    assert node._sub is None
    node._change_processing(True)
    assert node.type_to_model['YOLO'].call_count == 2
    assert model.set_classes.call_count == 2
    model.set_classes.assert_called_with(['person', 'box'])
    node.create_service.assert_called_once()


def test_failed_load_can_be_retried_without_subscribing_or_claiming_ready(wrapper):
    """A model load failure is visible to Manager and doesn't leave RGB active."""
    node, model = _lifecycle_instance(wrapper)
    node.type_to_model['YOLO'].side_effect = FileNotFoundError('cached.pt')
    response = node._set_processing(SimpleNamespace(data=True), SimpleNamespace())
    assert not response.success and 'load failed' in response.message
    assert not node._processing_enabled
    assert not hasattr(node, 'yolo')
    node.create_subscription.assert_not_called()
    node.type_to_model['YOLO'].side_effect = None
    assert node._set_processing(SimpleNamespace(data=True), SimpleNamespace()).success
    assert node.yolo is model


def test_vendor_enable_does_not_override_manager_idle(wrapper):
    """External enable remains compatible without forcing idle detector awake."""
    node, _ = _lifecycle_instance(wrapper)
    assert node.enable_cb(SimpleNamespace(data=True), SimpleNamespace()).success
    node.type_to_model['YOLO'].assert_not_called()
    node._change_processing(True)
    assert node.enable_cb(SimpleNamespace(data=False), SimpleNamespace()).success
    assert node._sub is None
    assert node._processing_enabled
    assert node.enable_cb(SimpleNamespace(data=True), SimpleNamespace()).success
    assert node._sub is not None
    node.type_to_model['YOLO'].assert_called_once()


@pytest.mark.parametrize('kind', ['World', 'YOLOE'])
def test_open_vocabulary_classes_can_be_set_before_first_processing(wrapper, monkeypatch, kind):
    """Keep the class-selection endpoint available even while weights are deferred."""
    node, model = _lifecycle_instance(wrapper)
    node.model_type = kind
    node.type_to_model = {kind: node.type_to_model['YOLO']}
    node.get_parameter = lambda _: SimpleNamespace(value=False)
    monkeypatch.setattr(wrapper.LifecycleNode, 'on_activate', lambda *_: None)
    assert node.on_activate(None) == wrapper.TransitionCallbackReturn.SUCCESS
    node.type_to_model[kind].assert_not_called()
    assert node.create_service.call_args_list[-1].args[1] == 'set_classes'
    node.set_classes_cb(SimpleNamespace(classes=['person']), object())
    assert node.yolo is model
    model.set_classes.assert_called_once_with(['person'])
    assert node._sub is None
