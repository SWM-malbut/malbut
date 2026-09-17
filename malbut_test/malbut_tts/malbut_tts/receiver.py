"""Receive Agent response text without synthesizing or playing audio."""

import argparse
import json
import sys
from typing import Optional, Sequence


RESPONSE_TOPIC = '/malbut/speech/response'


def receive_text(text: str, logger) -> bool:
    """Log each nonblank response verbatim as a JSON-escaped text value."""
    if not isinstance(text, str) or not text.strip():
        logger.warning('tts_text_ignored: blank response')
        return False
    logger.info(json.dumps(
        {'event': 'tts_text_received', 'text': text},
        ensure_ascii=False,
    ))
    return True


def create_receiver_node():
    """Create the ROS subscriber only when this receiver is activated."""
    from malbut_interfaces.msg import SpeechRequest
    from rclpy.node import Node
    from rclpy.qos import (
        DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy,
    )

    node = Node('malbut_tts_receiver')
    qos = QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=10,
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.VOLATILE,
    )
    try:
        node.create_subscription(
            SpeechRequest,
            RESPONSE_TOPIC,
            lambda message: receive_text(message.text, node.get_logger()),
            qos,
        )
    except Exception:
        node.destroy_node()
        raise
    return node


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run receipt-only TTS communication until interrupted."""
    parser = argparse.ArgumentParser(description=__doc__)
    _, ros_args = parser.parse_known_args(argv)
    try:
        import rclpy
        from rclpy.executors import ExternalShutdownException
    except ImportError:
        print('ROS 2 rclpy is required; source the ROS environment.',
              file=sys.stderr)
        return 2

    node = None
    initialized = False
    try:
        rclpy.init(args=ros_args)
        initialized = True
        node = create_receiver_node()
        node.get_logger().info(
            f'Listening on {RESPONSE_TOPIC}; text receipt only, no audio.'
        )
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    except ImportError:
        print('TTS receiver startup failed: ROS message dependency missing.',
              file=sys.stderr)
        return 2
    finally:
        if node is not None:
            node.destroy_node()
        if initialized and rclpy.ok():
            rclpy.shutdown()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
