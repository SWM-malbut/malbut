"""Own high-bandwidth subscriptions on one executor, including sleep/wake changes."""

import threading

from rclpy.executors import ExternalShutdownException, SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data


class SensorInputs:
    """Never destroy a subscription selected by another executor worker."""

    def __init__(self, owner, specs):
        self.owner = owner
        self.specs = [(name, kind, owner.resolve_topic_name(topic), callback)
                      for name, kind, topic, callback in specs]
        self._lock = threading.Lock()
        self._enabled = False
        self._closing = False
        self._ready = threading.Event()
        self.error = None
        self.executor = None
        self._guard = None
        self._thread = threading.Thread(target=self._run, name='patrol-sensors')
        self._thread.start()
        if not self._ready.wait(5.0) or self.error:
            self.close()
            raise RuntimeError(self.error or 'patrol sensor executor did not start')

    def set_enabled(self, enabled):
        """Coalesce demand; changes run between callbacks on the sensor thread."""
        with self._lock:
            if self._closing:
                return
            if self.error:
                raise RuntimeError(self.error)
            self._enabled = bool(enabled)
            self._guard.trigger()

    def _run(self):
        node = None
        try:
            node = Node(self.owner.get_name() + '_sensors', context=self.owner.context,
                        namespace=self.owner.get_namespace(), enable_rosout=False,
                        start_parameter_services=False, use_global_arguments=False)
            self.executor = SingleThreadedExecutor(context=self.owner.context)
            self.executor.add_node(node)

            def reconcile():
                with self._lock:
                    enabled = self._enabled and not self._closing
                for attribute, kind, topic, callback in self.specs:
                    subscription = getattr(self.owner, attribute)
                    if enabled and subscription is None:
                        setattr(self.owner, attribute, node.create_subscription(
                            kind, topic, callback, qos_profile_sensor_data))
                    elif not enabled and subscription is not None:
                        node.destroy_subscription(subscription)
                        setattr(self.owner, attribute, None)

            self._guard = node.create_guard_condition(reconcile)
            self._ready.set()
            while node.context.ok():
                with self._lock:
                    if self._closing:
                        break
                self.executor.spin_once(timeout_sec=1.0)
        except ExternalShutdownException:
            pass
        except Exception as error:
            self.error = str(error)
        finally:
            with self._lock:
                self._guard = None
            self._ready.set()
            if self.executor is not None:
                self.executor.shutdown()
            if node is not None:
                node.destroy_node()
            for attribute, *_ in self.specs:
                setattr(self.owner, attribute, None)

    def close(self):
        """Stop only this executor; never shut down the shared ROS context."""
        with self._lock:
            self._closing = True
            if self._guard is not None and self._thread.is_alive():
                self._guard.trigger()
        self._thread.join(timeout=5.0)
        if self._thread.is_alive():
            raise RuntimeError('patrol sensor executor did not stop')
