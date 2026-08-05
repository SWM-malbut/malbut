"""High-level robot tools exposed to language models."""

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List


@dataclass(frozen=True)
class ToolSpec:
    """One strict function schema."""

    name: str
    description: str
    parameters: Dict[str, Any]

    def to_openai_dict(self) -> Dict[str, Any]:
        """Return a Responses API strict function tool."""
        return {
            'type': 'function',
            'name': self.name,
            'description': self.description,
            'strict': True,
            'parameters': self.parameters,
        }


EMPTY_PARAMETERS: Dict[str, Any] = {
    'type': 'object',
    'properties': {},
    'required': [],
    'additionalProperties': False,
}


TOOL_SPECS = {
    'navigate': ToolSpec(
        name='navigate',
        description=(
            'Move through the verified Nav2 stack to one named indoor '
            'destination. Never use this for raw motor control.'
        ),
        parameters={
            'type': 'object',
            'properties': {
                'location': {
                    'type': 'string',
                    'description': (
                        'A named destination such as 거실, 주방, 침실, '
                        '현관, or 충전소.'
                    ),
                },
            },
            'required': ['location'],
            'additionalProperties': False,
        },
    ),
    'detect_pet': ToolSpec(
        name='detect_pet',
        description=(
            'Inspect the current camera view for a pet without moving.'
        ),
        parameters=EMPTY_PARAMETERS,
    ),
    'capture_photo': ToolSpec(
        name='capture_photo',
        description=(
            'Capture one still image from the camera after privacy checks.'
        ),
        parameters=EMPTY_PARAMETERS,
    ),
    'send_notification': ToolSpec(
        name='send_notification',
        description=(
            'Send a short notification to the registered caregiver.'
        ),
        parameters={
            'type': 'object',
            'properties': {
                'message': {
                    'type': 'string',
                    'description': 'Short notification text.',
                },
                'image_id': {
                    'type': ['string', 'null'],
                    'description': (
                        'A previously captured image identifier, or null.'
                    ),
                },
            },
            'required': ['message', 'image_id'],
            'additionalProperties': False,
        },
    ),
    'get_robot_status': ToolSpec(
        name='get_robot_status',
        description=(
            'Read battery and subsystem status without changing robot state.'
        ),
        parameters=EMPTY_PARAMETERS,
    ),
}


def select_tool_specs(names: Iterable[str]) -> List[ToolSpec]:
    """Return registered specs in request order, ignoring unknown names."""
    return [
        TOOL_SPECS[name]
        for name in names
        if name in TOOL_SPECS
    ]
