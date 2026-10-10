"""Validate structured voice proposals for the public Manager Action.

The LLM interprets intent; this policy validates tools, destinations and TTL.
It authorizes a bounded request to Manager, never physical readiness.
It does not turn an unknown RobotState into trusted sensor evidence. Existing
non-speech action policy and HTTP proposal behavior remain separate.
"""

import unicodedata

from malbut_agent_server.gateway import (
    CapabilityRegistry, PROPOSAL_ONLY, TOOL_TIMEOUT_SECONDS, ToolCapability,
)
from malbut_agent_server.safety import SafetyResult
from malbut_agent_server.schemas import SpeechAgentRequest, ValidationError
from malbut_agent_server.speech_navigation import matches_navigation_location
from malbut_agent_server.tools import (
    SPEECH_MISSION_TOOLS, TOOL_SPECS, validate_tool_arguments,
)


POLICY_REVISION = 'speech-manager-structured-v4'


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
        """Validate proposal bounds and name matching without inventing readiness."""
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
        if (decision.tool_name == 'request_navigation'
                and isinstance(request, SpeechAgentRequest)
                and request.navigation_locations is not None):
            locations = request.navigation_locations
            location = unicodedata.normalize('NFC', decision.arguments['location'].strip())
            if not locations:
                return SafetyResult(False, 'navigation_unavailable',
                                    '사용 중인 저장 지도와 등록된 목적지를 확인할 수 없어요.')
            if location not in locations:
                return SafetyResult(False, 'navigation_unknown',
                                    '등록된 목적지 이름을 확인해 다시 말씀해 주세요.')
            if (request.navigation_confirmation != location
                    and not matches_navigation_location(request.utterance, location, locations)):
                return SafetyResult(False, 'navigation_confirmation_required',
                                    f'“{location}”을 말씀하신 건가요? 그곳으로 이동할까요?')
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
