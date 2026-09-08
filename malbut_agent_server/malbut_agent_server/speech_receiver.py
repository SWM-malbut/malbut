"""Receive final STT messages for inspection, without invoking the Agent."""

import argparse
import json
import sqlite3
import sys
from typing import Optional, Sequence

from malbut_agent_server.speech_receipts import SpeechReceiptStore


TRANSCRIPT_TOPIC = '/malbut/speech/transcript'
DEFAULT_DB_PATH = '~/.local/state/malbut/speech-receipts.sqlite3'


def receive_transcript(receipts, utterance_id, text, logger) -> str:
    """Log a committed receipt and keep failures out of the success path."""
    try:
        outcome = receipts.receive(utterance_id, text)
    except ValueError:
        logger.warning('speech_transcript invalid: blank ID or text')
        return 'invalid'
    except sqlite3.Error:
        logger.error('speech_transcript storage_error: receipt not confirmed')
        return 'storage_error'

    event = {'event': 'speech_transcript', 'status': outcome,
             'utterance_id': utterance_id}
    if outcome == 'received':
        event['text'] = text
    message = json.dumps(event, ensure_ascii=False)
    if outcome == 'conflict':
        logger.warning(message)
    else:
        logger.info(message)
    return outcome


def create_receiver_node(receipts):
    """Create the opt-in ROS subscriber using the shared message type."""
    from malbut_interfaces.msg import SpeechTranscript
    from rclpy.node import Node
    from rclpy.qos import (
        DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy,
    )

    node = Node('malbut_agent_speech_receiver')
    qos = QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=10,
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.VOLATILE,
    )
    try:
        node.create_subscription(
            SpeechTranscript,
            TRANSCRIPT_TOPIC,
            lambda message: receive_transcript(
                receipts, message.utterance_id, message.text,
                node.get_logger(),
            ),
            qos,
        )
    except Exception:
        node.destroy_node()
        raise
    return node


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run receipt-only ROS communication until interrupted."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db-path', default=DEFAULT_DB_PATH)
    args, ros_args = parser.parse_known_args(argv)
    try:
        import rclpy
        from rclpy.executors import ExternalShutdownException
    except ImportError:
        print('ROS 2 rclpy is required; source the ROS environment.',
              file=sys.stderr)
        return 2

    receipts = None
    node = None
    initialized = False
    try:
        rclpy.init(args=ros_args)
        initialized = True
        receipts = SpeechReceiptStore(args.db_path)
        node = create_receiver_node(receipts)
        node.get_logger().info(
            f'Listening on {TRANSCRIPT_TOPIC}; receipt-only, no inference.'
        )
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    except (ImportError, OSError, sqlite3.Error) as error:
        print(f'Speech receiver startup failed: {type(error).__name__}',
              file=sys.stderr)
        return 2
    finally:
        if node is not None:
            node.destroy_node()
        if receipts is not None:
            receipts.close()
        if initialized and rclpy.ok():
            rclpy.shutdown()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
