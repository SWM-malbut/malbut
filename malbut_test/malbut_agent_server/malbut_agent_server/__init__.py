"""Provider-neutral agent boundary for the Malbut robot."""

from importlib import import_module
from typing import Any

__version__ = '0.5.0'

__all__ = [
    'AgentDecision',
    'AgentOrchestrator',
    'AgentRequest',
    'RobotState',
    'ValidationError',
]


_LAZY_EXPORTS = {
    'AgentOrchestrator': (
        'malbut_agent_server.orchestrator',
        'AgentOrchestrator',
    ),
    'AgentDecision': ('malbut_agent_server.schemas', 'AgentDecision'),
    'AgentRequest': ('malbut_agent_server.schemas', 'AgentRequest'),
    'RobotState': ('malbut_agent_server.schemas', 'RobotState'),
    'ValidationError': (
        'malbut_agent_server.schemas',
        'ValidationError',
    ),
}


def __getattr__(name: str) -> Any:
    """Load compatibility exports only when callers request them."""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    module_name, attribute_name = target
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Expose lazy compatibility names to interactive tooling."""
    return sorted(set(globals()) | set(__all__))
