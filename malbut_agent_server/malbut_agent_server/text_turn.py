"""Authenticated text routing for proposal and deterministic confirmation."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from malbut_agent_server.conversation import (
    BeginTurnToken,
    ConfirmationIntentNotFoundError,
    TextTurnRequestClaim,
)
from malbut_agent_server.named_target import NamedTargetResolver
from malbut_agent_server.orchestrator import (
    AgentOrchestrator,
    OrchestrationResult,
)
from malbut_agent_server.safety import SafetyResult
from malbut_agent_server.schemas import (
    MAX_ID_LENGTH,
    MAX_UTTERANCE_LENGTH,
    AgentDecision,
    AgentRequest,
    RobotState,
    ValidationError,
    validate_conversation_id,
    validate_turn_id,
    validate_user_id,
)
from malbut_agent_server.text_confirmation import (
    APPROVED,
    CANCELED,
    DENIED,
    INVALIDATED,
    ConfirmationDraft,
    ConfirmationRecord,
    ConfirmationResolution,
    classify_confirmation_text,
)


@dataclass(frozen=True)
class TextTurnRequest:
    """Minimal body whose user and robot state remain server-owned."""

    request_id: str
    conversation_id: str
    turn_id: str
    text: str

    @classmethod
    def from_dict(cls, value: Any) -> 'TextTurnRequest':
        """Reject identity, state, approval, and execution injection."""
        if type(value) is not dict:
            raise ValidationError('text turn body must be an object')
        allowed = {
            'request_id',
            'conversation_id',
            'turn_id',
            'text',
        }
        unknown = set(value) - allowed
        if unknown:
            names = ', '.join(sorted(unknown))
            raise ValidationError(
                f'text turn contains unknown fields: {names}'
            )
        request_id = _text(
            value.get('request_id'),
            'request_id',
            MAX_ID_LENGTH,
        )
        if any(
            ord(character) < 32 or ord(character) == 127
            for character in request_id
        ):
            raise ValidationError(
                'request_id must not contain control characters'
            )
        return cls(
            request_id=request_id,
            conversation_id=validate_conversation_id(
                value.get('conversation_id')
            ),
            turn_id=validate_turn_id(value.get('turn_id')),
            text=_text(
                value.get('text'),
                'text',
                MAX_UTTERANCE_LENGTH,
            ),
        )

    def fingerprint(self) -> str:
        """Bind the full normalized HTTP text-turn envelope."""
        canonical = json.dumps(
            {
                'schema_version': 1,
                'request_id': self.request_id,
                'conversation_id': self.conversation_id,
                'turn_id': self.turn_id,
                'text': self.text,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        )
        return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


class TextTurnService:
    """Keep pending responses out of the LLM and out of execution code."""

    def __init__(
        self,
        orchestrator: AgentOrchestrator,
        target_resolver: NamedTargetResolver,
        *,
        clock: Callable[[], float] = time.time,
        maximum_confirmation_seconds: float = 30.0,
        create_robot_actions: bool = False,
        action_dispatch_window_seconds: float = 30.0,
    ) -> None:
        """Bind proposal, state, target, and durable confirmation services."""
        if not isinstance(orchestrator, AgentOrchestrator):
            raise TypeError('orchestrator must be an AgentOrchestrator')
        if not callable(getattr(target_resolver, 'resolve', None)):
            raise TypeError('target_resolver must implement resolve')
        if not callable(clock):
            raise TypeError('clock must be callable')
        if type(maximum_confirmation_seconds) not in {int, float} or not (
            1.0 <= float(maximum_confirmation_seconds) <= 120.0
        ):
            raise ValueError(
                'maximum_confirmation_seconds must be from 1 to 120'
            )
        if type(create_robot_actions) is not bool:
            raise TypeError('create_robot_actions must be a boolean')
        if (
            type(action_dispatch_window_seconds) not in {int, float}
            or not 1.0 <= float(action_dispatch_window_seconds) <= 120.0
        ):
            raise ValueError(
                'action_dispatch_window_seconds must be from 1 to 120'
            )
        self.orchestrator = orchestrator
        self.store = orchestrator.conversation_store
        self.target_resolver = target_resolver
        self.clock = clock
        self.maximum_confirmation_seconds = float(
            maximum_confirmation_seconds
        )
        self.create_robot_actions = create_robot_actions
        self.action_dispatch_window_seconds = float(
            action_dispatch_window_seconds
        )

    def handle(self, *, user_id: str, value: Any) -> dict[str, Any]:
        """Handle exactly one normal or confirmation text input."""
        owner = validate_user_id(user_id)
        request = TextTurnRequest.from_dict(value)
        request_fingerprint = request.fingerprint()
        classified = classify_confirmation_text(request.text)

        claimed = self.store.text_turn_request_claim(
            owner,
            request.request_id,
            request_fingerprint,
        )
        if claimed is not None:
            return self._replay_claim(request, *claimed)

        replay = self._confirmation_for_response(
            owner,
            request.request_id,
        )
        if replay is not None:
            return self._replay_response(owner, request, replay)

        if self.store.has_agent_request(owner, request.request_id):
            return self._handle_new_request(owner, request)

        pending = self.store.pending_confirmation(
            owner,
            request.conversation_id,
        )
        if pending is not None:
            return self._handle_pending(
                owner,
                request,
                pending,
                classified,
                request_fingerprint,
            )
        if classified is not None:
            self.store.claim_text_turn_response(
                owner,
                request.conversation_id,
                request_id=request.request_id,
                turn_id=request.turn_id,
                request_fingerprint=request_fingerprint,
                outcome='confirmation_not_pending',
                now=float(self.clock()),
            )
            return self._no_pending_response(request)
        return self._handle_new_request(owner, request)

    def _replay_claim(
        self,
        request: TextTurnRequest,
        claim: TextTurnRequestClaim,
        record: ConfirmationRecord | None,
    ) -> dict[str, Any]:
        if claim.outcome == 'confirmation_not_pending':
            if record is not None:
                raise RuntimeError(
                    'no-pending claim unexpectedly has a confirmation'
                )
            return self._no_pending_response(request)
        if record is None:
            raise RuntimeError('confirmation claim lost its record')
        if claim.outcome == 'confirmation_unrecognized':
            pending = ConfirmationRecord.pending(record.draft)
            value = self._record_response(
                request,
                pending,
                cached=True,
            )
            value['result_code'] = 'confirmation_response_unrecognized'
            value['message'] = (
                '네, 아니요, 또는 취소 중 하나로 답해 주세요.'
            )
            return value
        if claim.outcome in {
            'confirmation_resolved',
            'confirmation_invalidated',
        }:
            return self._record_response(
                request,
                record,
                cached=True,
            )
        raise RuntimeError('text turn claim outcome is unsupported')

    def _confirmation_for_response(
        self,
        user_id: str,
        response_id: str,
    ) -> ConfirmationRecord | None:
        return self.store.confirmation_for_response(
            user_id,
            response_id,
        )

    def _replay_response(
        self,
        user_id: str,
        request: TextTurnRequest,
        record: ConfirmationRecord,
    ) -> dict[str, Any]:
        resolution = ConfirmationResolution.create(
            record,
            caller_user_id=user_id,
            caller_conversation_id=request.conversation_id,
            caller_session_instance_id=record.session_instance_id,
            caller_generation=record.generation,
            response_id=request.request_id,
            response_turn_id=request.turn_id,
            response_text=request.text,
        )
        terminal = record.resolve(
            resolution,
            resolved_at=record.resolved_at or float(self.clock()),
        )
        return self._record_response(
            request,
            terminal,
            cached=True,
        )

    def _handle_pending(
        self,
        user_id: str,
        request: TextTurnRequest,
        record: ConfirmationRecord,
        classified: str | None,
        request_fingerprint: str,
    ) -> dict[str, Any]:
        if classified is None:
            self.store.claim_text_turn_response(
                user_id,
                request.conversation_id,
                request_id=request.request_id,
                turn_id=request.turn_id,
                request_fingerprint=request_fingerprint,
                outcome='confirmation_unrecognized',
                confirmation_request_id=(
                    record.confirmation_request_id
                ),
                now=float(self.clock()),
            )
            value = self._record_response(request, record, cached=False)
            value['result_code'] = 'confirmation_response_unrecognized'
            value['message'] = (
                '네, 아니요, 또는 취소 중 하나로 답해 주세요.'
            )
            return value

        current_target = self._resolve_record_target(record)
        if (
            current_target is None
            or current_target.binding_digest
            != record.target_binding_digest
        ):
            invalidated = self._invalidate_target_change(
                user_id,
                request.conversation_id,
                record,
                request,
                request_fingerprint,
            )
            return self._record_response(
                request,
                invalidated,
                cached=False,
            )

        resolution = ConfirmationResolution.create(
            record,
            caller_user_id=user_id,
            caller_conversation_id=request.conversation_id,
            caller_session_instance_id=record.session_instance_id,
            caller_generation=record.generation,
            response_id=request.request_id,
            response_turn_id=request.turn_id,
            response_text=request.text,
        )
        terminal = self.store.resolve_confirmation(
            user_id,
            request.conversation_id,
            response_id=resolution.response_id,
            response_fingerprint=resolution.response_fingerprint,
            disposition=resolution.requested_disposition,
            now=float(self.clock()),
            current_target_binding_digest=(
                current_target.binding_digest
            ),
            response_turn_id=resolution.response_turn_id,
            text_turn_request_fingerprint=request_fingerprint,
            create_robot_action=self.create_robot_actions,
            action_dispatch_window_seconds=(
                self.action_dispatch_window_seconds
            ),
        )
        return self._record_response(
            request,
            terminal,
            cached=False,
        )

    def _resolve_record_target(
        self,
        record: ConfirmationRecord,
    ) -> Any | None:
        try:
            location = record.arguments_dict()['location']
            return self.target_resolver.resolve(location)
        except Exception:
            return None

    def _invalidate_target_change(
        self,
        user_id: str,
        conversation_id: str,
        record: ConfirmationRecord,
        request: TextTurnRequest,
        request_fingerprint: str,
    ) -> ConfirmationRecord:
        return self.store.invalidate_confirmation(
            user_id,
            conversation_id,
            result_code='confirmation_target_changed',
            now=float(self.clock()),
            expected_target_binding_digest=(
                record.target_binding_digest
            ),
            response_id=request.request_id,
            response_turn_id=request.turn_id,
            text_turn_request_fingerprint=request_fingerprint,
        )

    def _handle_new_request(
        self,
        user_id: str,
        request: TextTurnRequest,
    ) -> dict[str, Any]:
        agent_request = AgentRequest(
            request_id=request.request_id,
            user_id=user_id,
            conversation_id=request.conversation_id,
            turn_id=request.turn_id,
            utterance=request.text,
            robot_state=RobotState(),
            available_tools=('navigate',),
        )
        result = self.orchestrator.handle(
            agent_request,
            confirmation_factory=self._confirmation_factory,
        )
        try:
            confirmation = self.store.confirmation_for_request(
                user_id,
                request.request_id,
            )
        except ConfirmationIntentNotFoundError:
            value = result.to_dict()
            value['schema_version'] = 1
            value['status'] = 'completed'
            value['execution']['execution_authorized'] = False
            value['execution']['physical_authorized'] = False
            value['execution']['nav2_start_count'] = 0
            value['execution']['nav2_cancel_count'] = 0
            return value
        return self._record_response(
            request,
            confirmation,
            cached=False,
        )

    def _confirmation_factory(
        self,
        result: OrchestrationResult,
        token: BeginTurnToken,
    ) -> ConfirmationDraft | None:
        decision = result.decision
        if (
            decision.type != 'tool_call'
            or decision.tool_name != 'navigate'
            or result.safety.allowed is not True
            or result.state_trusted is not True
        ):
            return None
        try:
            location = decision.arguments['location']
            target = self.target_resolver.resolve(location)
        except Exception:
            self._reject_unresolved_target(result)
            return None
        return ConfirmationDraft.from_orchestration(
            result,
            token,
            target,
            confirmation_expires_at=(
                result.issued_at + self.maximum_confirmation_seconds
            ),
        )

    @staticmethod
    def _reject_unresolved_target(result: OrchestrationResult) -> None:
        reason = (
            '현재 지도에서 해당 목적지를 확인할 수 없어 '
            '이동하지 않습니다.'
        )
        result.safety = SafetyResult(
            False,
            'named_target_unavailable',
            reason,
        )
        result.decision = AgentDecision(
            type='refusal',
            message=reason,
            reason='safety:named_target_unavailable',
            confidence=1.0,
            expires_in_ms=result.decision.expires_in_ms,
        )

    def _record_response(
        self,
        request: TextTurnRequest,
        record: ConfirmationRecord,
        *,
        cached: bool,
    ) -> dict[str, Any]:
        value = record.to_public_dict()
        value.update({
            'schema_version': 1,
            'request_id': request.request_id,
            'turn_id': request.turn_id,
            'conversation': {
                'conversation_id': record.conversation_id,
                'generation': record.generation,
                'revision': record.revision,
            },
            'cached': cached,
        })
        if record.disposition == APPROVED:
            # Keep this response independent from the current process mode.
            # The same durable confirmation can be replayed after restart, so
            # neither "queued" nor "not started" can be inferred here.
            value['message'] = (
                '승인을 기록했습니다. 이 응답 자체는 이동 실행 권한이 '
                '아니며, 이동 여부는 별도 안전 재검사에서 결정됩니다.'
            )
        elif record.disposition == DENIED:
            value['message'] = '요청을 거절했습니다.'
        elif record.disposition == CANCELED:
            value['message'] = '요청을 취소했습니다.'
        elif record.disposition == INVALIDATED:
            value['message'] = (
                '목적지 정보가 바뀌어 기존 확인을 무효화했습니다.'
            )
        return value

    @staticmethod
    def _no_pending_response(
        request: TextTurnRequest,
    ) -> dict[str, Any]:
        return {
            'schema_version': 1,
            'request_id': request.request_id,
            'turn_id': request.turn_id,
            'conversation': {
                'conversation_id': request.conversation_id,
            },
            'status': 'no_pending_confirmation',
            'result_code': 'confirmation_not_pending',
            'message': '현재 확인할 요청이 없습니다.',
            'execution': {
                'authorized': False,
                'execution_authorized': False,
                'consume_once': False,
                'tool_call_id': None,
                'physical_authorized': False,
                'nav2_start_count': 0,
                'nav2_cancel_count': 0,
            },
        }


def _text(value: Any, field_name: str, maximum: int) -> str:
    if type(value) is not str:
        raise ValidationError(f'{field_name} must be a string')
    result = value.strip()
    if not result or len(result) > maximum:
        raise ValidationError(f'{field_name} is invalid')
    if any(
        ord(character) < 32 and character not in '\n\t'
        for character in result
    ):
        raise ValidationError(f'{field_name} contains control characters')
    return result
