"""Tests for detector process exit semantics."""

import homecam_detector.detector_node as detector_node


def test_startup_configuration_error_returns_nonzero(monkeypatch, capsys) -> None:
    monkeypatch.setattr(detector_node.rclpy, "init", lambda args=None: None)
    monkeypatch.setattr(detector_node.rclpy, "ok", lambda: False)

    def fail_startup():
        raise ValueError("bad configuration")

    monkeypatch.setattr(detector_node, "HomecamDetectorNode", fail_startup)
    assert detector_node.main([]) == 2
    assert "startup failed: bad configuration" in capsys.readouterr().err


def test_keyboard_interrupt_remains_clean(monkeypatch) -> None:
    class FakeNode:
        destroyed = False

        def destroy_node(self):
            self.destroyed = True

    node = FakeNode()
    monkeypatch.setattr(detector_node.rclpy, "init", lambda args=None: None)
    monkeypatch.setattr(detector_node.rclpy, "ok", lambda: False)
    monkeypatch.setattr(detector_node, "HomecamDetectorNode", lambda: node)

    def interrupt(_node):
        raise KeyboardInterrupt

    monkeypatch.setattr(detector_node.rclpy, "spin", interrupt)
    assert detector_node.main([]) == 0
    assert node.destroyed


def test_external_ros_shutdown_remains_clean(monkeypatch) -> None:
    class FakeNode:
        destroyed = False

        def destroy_node(self):
            self.destroyed = True

    node = FakeNode()
    monkeypatch.setattr(detector_node.rclpy, "init", lambda args=None: None)
    monkeypatch.setattr(detector_node.rclpy, "ok", lambda: False)
    monkeypatch.setattr(detector_node, "HomecamDetectorNode", lambda: node)

    def shutdown(_node):
        raise detector_node.ExternalShutdownException

    monkeypatch.setattr(detector_node.rclpy, "spin", shutdown)
    assert detector_node.main([]) == 0
    assert node.destroyed
