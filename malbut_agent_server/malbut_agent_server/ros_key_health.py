"""Share how a key is doing on /malbut/keys/health so key_sync can tell the web.

Messages are std_msgs/String JSON: {"service":"openai","state":"invalid","code":"authentication_failed"}.
Never the key itself. Transient local, so key_sync hears the latest even if it starts later.
"""

import json
from threading import RLock

HEALTH_TOPIC = '/malbut/keys/health'


def health_qos():
    from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
    return QoSProfile(depth=4, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                      reliability=ReliabilityPolicy.RELIABLE)


def health_message(service, state, code):
    return json.dumps({'service': service, 'state': state, 'code': code}, separators=(',', ':'))


class KeyHealthPublisher:
    """Publish a ManagedKey's health whenever it changes."""

    def __init__(self, node, managed_key):
        from std_msgs.msg import String
        self._lock = RLock()
        self._closed = False
        self._key = managed_key
        self._string = String
        self._publisher = node.create_publisher(String, HEALTH_TOPIC, health_qos())
        managed_key.add_listener(self._publish)

    def _publish(self, service, state, code):
        with self._lock:
            if self._closed:
                return
            message = self._string()
            message.data = health_message(service, state, code)
            self._publisher.publish(message)

    def close(self):
        """Drain an in-flight publication before ROS destroys its publisher."""
        with self._lock:
            self._closed = True
            self._key.remove_listener(self._publish)
