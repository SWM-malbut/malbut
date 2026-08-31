"""Server-owned routing policy for text-agent model decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from malbut_agent_server.gateway import (
    PROPOSAL_ONLY,
    READ_ONLY,
    TOOL_RISK_LEVELS,
    CapabilityRegistry,
)
from malbut_agent_server.schemas import AgentDecision, ValidationError
from malbut_agent_server.tools import validate_tool_arguments


class TextDecisionRoute(str, Enum):
    """One server-derived route; never a model-provided authority field."""

    DIRECT_REPLY = 'direct_reply'
    READ_ONLY_QUERY = 'read_only_query'
    CONFIRMABLE_ACTION_PROPOSAL = 'confirmable_action_proposal'
    REJECTED = 'rejected'


@dataclass(frozen=True)
class TextDecisionClassification:
    """Bounded result of applying the current capability policy."""

    route: TextDecisionRoute
    code: str
    tool_name: str | None = None
    capability_mode: str | None = None
    risk_level: str | None = None
    capability_revision: str | None = None

    @property
    def confirmable(self) -> bool:
        """Return whether this is only eligible to request confirmation."""
        return self.route is TextDecisionRoute.CONFIRMABLE_ACTION_PROPOSAL


class TextDecisionPolicy:
    """Derive text routes from trusted registry metadata and strict schemas."""

    def __init__(
        self,
        registry: CapabilityRegistry,
        *,
        confirmable_tool_names: Iterable[str],
    ) -> None:
        """Capture the server-owned set of confirmation-capable actions."""
        if not isinstance(registry, CapabilityRegistry):
            raise TypeError('registry must be a CapabilityRegistry')
        if isinstance(confirmable_tool_names, (str, bytes)):
            raise TypeError(
                'confirmable_tool_names must be an iterable of names'
            )
        names = []
        for name in confirmable_tool_names:
            if type(name) is not str or not name.strip():
                raise ValueError('confirmable Tool name is invalid')
            normalized = name.strip()
            if normalized in names:
                continue
            names.append(normalized)
        self.registry = registry
        self.confirmable_tool_names = frozenset(names)

    def classify(
        self,
        decision: AgentDecision,
        *,
        available_tools: Iterable[str],
    ) -> TextDecisionClassification:
        """Classify one untrusted proposal without authorizing execution."""
        if not isinstance(decision, AgentDecision):
            raise TypeError('decision must be an AgentDecision')
        try:
            decision.validate()
        except (TypeError, ValidationError):
            return self._rejected('decision_invalid')

        if decision.type != 'tool_call':
            return TextDecisionClassification(
                route=TextDecisionRoute.DIRECT_REPLY,
                code='direct_reply',
                capability_revision=self.registry.revision,
            )

        tool_name = decision.tool_name
        capability = self.registry.get(tool_name or '')
        if capability is None:
            return self._rejected(
                'unknown_tool',
                tool_name=tool_name,
            )

        effective_names = self._effective_names(available_tools)
        if not capability.available or tool_name not in effective_names:
            return self._rejected(
                'tool_unavailable',
                tool_name=tool_name,
                capability_mode=capability.mode,
            )

        try:
            validate_tool_arguments(tool_name, decision.arguments)
        except (TypeError, ValidationError):
            return self._rejected(
                'invalid_arguments',
                tool_name=tool_name,
                capability_mode=capability.mode,
            )

        risk_level = TOOL_RISK_LEVELS[tool_name]
        if capability.mode == READ_ONLY:
            return TextDecisionClassification(
                route=TextDecisionRoute.READ_ONLY_QUERY,
                code='read_only_query',
                tool_name=tool_name,
                capability_mode=capability.mode,
                risk_level=risk_level,
                capability_revision=self.registry.revision,
            )
        if (
            capability.mode == PROPOSAL_ONLY
            and tool_name in self.confirmable_tool_names
        ):
            return TextDecisionClassification(
                route=(
                    TextDecisionRoute.CONFIRMABLE_ACTION_PROPOSAL
                ),
                code='confirmation_required',
                tool_name=tool_name,
                capability_mode=capability.mode,
                risk_level=risk_level,
                capability_revision=self.registry.revision,
            )
        return self._rejected(
            'tool_not_routable',
            tool_name=tool_name,
            capability_mode=capability.mode,
        )

    def _effective_names(
        self,
        available_tools: Iterable[str],
    ) -> frozenset[str]:
        if isinstance(available_tools, (str, bytes)):
            return frozenset()
        try:
            requested = tuple(available_tools)
        except TypeError:
            return frozenset()
        if any(type(name) is not str for name in requested):
            return frozenset()
        return frozenset(self.registry.effective_names(requested))

    def _rejected(
        self,
        code: str,
        *,
        tool_name: str | None = None,
        capability_mode: str | None = None,
    ) -> TextDecisionClassification:
        return TextDecisionClassification(
            route=TextDecisionRoute.REJECTED,
            code=code,
            tool_name=tool_name,
            capability_mode=capability_mode,
            risk_level=(
                TOOL_RISK_LEVELS.get(tool_name)
                if tool_name is not None
                else None
            ),
            capability_revision=self.registry.revision,
        )
