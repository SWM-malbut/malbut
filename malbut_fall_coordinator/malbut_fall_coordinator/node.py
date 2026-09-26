"""Own fall-specific settings and handoffs, not robot mission scheduling."""

import signal
from threading import Event

import rclpy
from rcl_interfaces.msg import ParameterDescriptor
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rclpy.node import Node
from rclpy.signals import SignalHandlerOptions

from .fall_confirmation_link import FallConfirmationLink
from .fall_settings_link import FallSettingsLink


class FallCoordinatorNode(Node):
    """Relay permissions and request urgent confirmation through ExecuteMission."""

    def __init__(self):
        super().__init__('fall_coordinator')
        ids = {
            key: self.declare_parameter(
                key, '', descriptor=ParameterDescriptor(read_only=True)).value
            for key in ('runtime_id', 'bridge_runtime_id', 'vlm_runtime_id')
        }
        # manager_runtime_id remains the settings wire field for compatibility;
        # it now identifies this coordinator, not the generic system manager.
        self.settings = FallSettingsLink(
            self, manager_id=ids['runtime_id'], bridge_id=ids['bridge_runtime_id'],
            vlm_id=ids['vlm_runtime_id'])
        self.confirmation = FallConfirmationLink(self, runtime_id=ids['vlm_runtime_id'])

    def close(self):
        """Stop permission heartbeats and cancel our managed confirmation."""
        self.settings.close()
        return self.confirmation.close()

    def destroy_node(self):
        """Release the Action client before destroying ROS entities."""
        self.close()
        self.confirmation.destroy()
        return super().destroy_node()


def main(args=None):
    """Keep ROS alive long enough to send cancellation on a normal shutdown."""
    rclpy.init(args=args, signal_handler_options=SignalHandlerOptions.NO)
    stopped = Event()
    previous = {sig: signal.signal(sig, lambda *_: stopped.set())
                for sig in (signal.SIGINT, signal.SIGTERM)}
    node = None
    executor = MultiThreadedExecutor(num_threads=2)
    try:
        node = FallCoordinatorNode()
        executor.add_node(node)
        while rclpy.ok() and not stopped.is_set():
            executor.spin_once(timeout_sec=0.1)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        if node is not None:
            pending = node.close()
            if pending is not None and rclpy.ok():
                executor.spin_until_future_complete(pending, timeout_sec=3.0)
        executor.shutdown()
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
