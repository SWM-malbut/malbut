"""Developer JSON-line stdin command parsing and execution."""

import json
import os
import select
from typing import Any, Dict, Iterator, Optional


MAX_COMMAND_BYTES = 65536


def apply_command(node: Any, command: Any) -> Dict[str, Any]:
    """Run a structured developer command, not natural speech."""
    if not isinstance(command, dict):
        raise ValueError('command must be a JSON object')
    op = command.get('op')
    allowed = {
        'say': {'op', 'text'},
        'submit': {'op', 'capability_id', 'arguments', 'request_id'},
        'cancel': {'op', 'request_id'},
        'status': {'op', 'request_id'},
    }
    if op not in allowed or set(command) - allowed[op]:
        raise ValueError('unsupported operation or command fields')
    if op == 'say':
        if not node.say(command.get('text')):
            raise ValueError('text must be non-empty and ROS must be running')
        return {'published': True}
    if op == 'submit':
        request_id = node.missions.submit(
            command.get('capability_id'), command.get('arguments'),
            request_id=command.get('request_id'),
        )
    else:
        request_id = command.get('request_id')
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError('request_id is required')
        if op == 'cancel':
            node.missions.cancel(request_id)
    return node.missions.snapshot(request_id)


class CommandLines:
    """Bound partial stdin input without blocking ROS feedback and cancel."""

    def __init__(self) -> None:
        """Start an empty command buffer."""
        self._buffer = bytearray()
        self._discarding = False

    def feed(self, data: bytes) -> Iterator[Optional[bytes]]:
        """Yield complete lines, with None for each oversized command."""
        for fragment in data.splitlines(keepends=True):
            complete = fragment.endswith(b'\n')
            if not self._discarding:
                if len(self._buffer) + len(fragment) > MAX_COMMAND_BYTES:
                    self._buffer.clear()
                    self._discarding = True
                    yield None
                else:
                    self._buffer.extend(fragment)
            if complete:
                if not self._discarding:
                    yield bytes(self._buffer)
                self._buffer.clear()
                self._discarding = False


def read_commands(
    node: Any,
    descriptor: Optional[int],
    lines: CommandLines,
) -> Optional[int]:
    """Apply complete stdin command lines; return None once stdin closes."""
    if descriptor is None or not select.select([descriptor], [], [], 0)[0]:
        return descriptor
    data = os.read(descriptor, 4096)
    if not data:
        return None
    for line in lines.feed(data):
        if line is None:
            node.get_logger().warning(
                'Command exceeds 65536 bytes; discarded.',
            )
            continue
        if not line.strip():
            continue
        try:
            result = apply_command(node, json.loads(line))
            node.get_logger().info(json.dumps(
                {'event': 'command_result', 'result': result},
                ensure_ascii=False,
            ))
        except (ValueError, KeyError, TypeError) as error:
            node.get_logger().warning('Invalid command: ' + str(error))
    return descriptor
