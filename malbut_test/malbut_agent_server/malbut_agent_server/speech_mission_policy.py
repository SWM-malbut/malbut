"""Validate structured voice proposals for the public Manager Action.

The LLM interprets intent; this policy validates enabled tools, arguments and TTL.
It authorizes a bounded request to Manager, never physical readiness.
It does not turn an unknown RobotState into trusted sensor evidence. Existing
non-speech action policy and HTTP proposal behavior remain separate.
"""

from malbut_agent_server.gateway import (
    CapabilityRegistry, PROPOSAL_ONLY, TOOL_TIMEOUT_SECONDS, ToolCapability,
)
from malbut_agent_server.safety import SafetyResult
from malbut_agent_server.schemas import ValidationError
from malbut_agent_server.tools import (
    SPEECH_MISSION_TOOLS, TOOL_SPECS, validate_tool_arguments,
)


POLICY_REVISION = 'speech-manager-structured-v3'


class SpeechMissionPolicy:
    """Validate structured Manager requests while retaining legacy action gates."""

    policy_revision = POLICY_REVISION

    def __init__(self, base_policy, enabled_tools):
        """Wrap the existing policy with explicitly enabled Manager requests."""
        self._base = base_policy
        self.enabled_tools = frozenset(enabled_tools)
        if not self.enabled_tools.issubset(SPEECH_MISSION_TOOLS):
            raise ValueError('unsupported speech mission tool')

    def __getattr__(self, name):
        """Retain the underlying policy's unrelated validation interfaces."""
        return getattr(self._base, name)

    def evaluate(self, request, decision, state_trusted=False):
        """Validate proposal bounds, without parsing language or inventing readiness."""
        if decision.type != 'tool_call' or decision.tool_name not in SPEECH_MISSION_TOOLS:
            return self._base.evaluate(request, decision, state_trusted=state_trusted)
        if (decision.tool_name not in self.enabled_tools
                or decision.tool_name not in request.available_tools):
            return SafetyResult(False, 'tool_unavailable', '이 음성 실행 기능이 연결되지 않았어요.')
        try:
            validate_tool_arguments(decision.tool_name, decision.arguments)
        except (ValidationError, TypeError):
            return SafetyResult(False, 'invalid_arguments', '실행 요청의 인자가 올바르지 않아요.')
        if (type(decision.expires_in_ms) is not int or decision.expires_in_ms <= 0
                or decision.expires_in_ms > self._base.maximum_action_ttl_ms):
            return SafetyResult(False, 'ttl_too_long', '실행 요청의 유효 시간이 올바르지 않아요.')
        return SafetyResult(
            True, 'manager_request',
            'Manager에 요청할 수 있어요. 실제 실행 가능 여부는 Manager가 판단해요.',
        )


def configure_speech_missions(runtime, *, navigation_enabled=False):
    """Explicitly enable Manager proposals on this speech runtime only.

    The caller must supply an actual named-target resolver before enabling
    navigation. No ROS object, hardware state, or execution adapter is created.
    ``runtime.speech_mission_tools`` is the allowed subset for voice requests.
    """
    if type(navigation_enabled) is not bool:
        raise TypeError('navigation_enabled must be a boolean')
    enabled = tuple(name for name in SPEECH_MISSION_TOOLS
                    if navigation_enabled or name != 'request_navigation')
    registry = runtime.capability_registry
    entries = []
    for name in TOOL_SPECS:
        entry = registry.get(name)
        if name in SPEECH_MISSION_TOOLS:
            entry = ToolCapability(
                name=name, mode=PROPOSAL_ONLY, available=name in enabled,
                timeout_seconds=TOOL_TIMEOUT_SECONDS[name],
            )
        if entry is not None:
            entries.append(entry)
    runtime.capability_registry = CapabilityRegistry(
        entries, runtime_mode=registry.runtime_mode,
        revision=POLICY_REVISION,
    )
    base = runtime.safety_policy
    if isinstance(base, SpeechMissionPolicy):
        base = base._base
    runtime.safety_policy = SpeechMissionPolicy(base, enabled)
    runtime.speech_mission_tools = enabled
    return runtime
