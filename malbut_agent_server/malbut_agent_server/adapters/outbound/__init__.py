"""Lazy compatibility exports for outbound adapters."""

from importlib import import_module
from typing import Any


__all__ = [
    'ActionClaimLostError',
    'ActionConflictError',
    'ActionPersistenceError',
    'SQLiteActionRepository',
    'initialize_action_schema',
    'insert_action_for_approved_confirmation',
]


_SQLITE_MODULE = (
    'malbut_agent_server.adapters.outbound.sqlite_action_repository'
)
_LAZY_EXPORTS = {
    name: (_SQLITE_MODULE, name)
    for name in __all__
}


def __getattr__(name: str) -> Any:
    """Load SQLite infrastructure only when explicitly requested."""
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
