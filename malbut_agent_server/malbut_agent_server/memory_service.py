"""Trusted service boundary for evidence-backed memory mutations."""

import math
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from malbut_agent_server.conversation import (
    ConversationNotFoundError,
    ConversationStateError,
    SQLiteConversationStore,
)
from malbut_agent_server.memory import (
    MemoryConsentError,
    MemoryMutationResult,
    SQLiteMemoryStore,
)
from malbut_agent_server.schemas import (
    ValidationError,
    validate_conversation_id,
    validate_turn_id,
    validate_user_id,
)


_UNSET = object()


class MemoryEvidenceError(ValidationError):
    """Raised when confirmation evidence is absent or not trustworthy."""


@dataclass(frozen=True)
class CompletedTurnEvidence:
    """Minimal trusted proof that one user's turn was completed."""

    user_id: str
    conversation_id: str
    turn_id: str
    session_instance_id: str
    generation: int
    completed_at: float


class CompletedTurnEvidenceValidator(Protocol):
    """Validate a completed conversation turn against a source of truth."""

    def validate_completed_turn(
        self,
        user_id: str,
        conversation_id: str,
        turn_id: str,
    ) -> CompletedTurnEvidence:
        """Return trusted evidence or raise ``MemoryEvidenceError``."""


class SQLiteConversationEvidenceValidator:
    """Resolve completed evidence through the conversation public API."""

    def __init__(
        self,
        conversation_store: SQLiteConversationStore,
    ) -> None:
        """Bind to the trusted conversation source of truth."""
        self._conversation_store = conversation_store

    def validate_completed_turn(
        self,
        user_id: str,
        conversation_id: str,
        turn_id: str,
    ) -> CompletedTurnEvidence:
        """Find one completed turn without crossing the user boundary."""
        normalized_user = validate_user_id(user_id)
        normalized_conversation = validate_conversation_id(
            conversation_id
        )
        normalized_turn = validate_turn_id(turn_id)
        try:
            turn = self._conversation_store.get_completed_turn(
                normalized_user,
                normalized_conversation,
                normalized_turn,
            )
        except (
            ConversationNotFoundError,
            ConversationStateError,
        ) as error:
            raise MemoryEvidenceError(
                'completed confirmation turn was not found'
            ) from error
        return CompletedTurnEvidence(
            user_id=turn.user_id,
            conversation_id=turn.conversation_id,
            turn_id=turn.turn_id,
            session_instance_id=turn.session_instance_id,
            generation=turn.generation,
            completed_at=turn.completed_at,
        )


class ConfirmedMemoryService:
    """Permit confirmed mutations only after trusted turn validation."""

    def __init__(
        self,
        memory_store: SQLiteMemoryStore,
        evidence_validator: CompletedTurnEvidenceValidator,
    ) -> None:
        """Bind the mutation store to its trusted evidence verifier."""
        self._memory_store = memory_store
        self._evidence_validator = evidence_validator

    def commit_confirmed(
        self,
        user_id: str,
        request_id: str,
        content: str,
        evidence_conversation_id: str,
        evidence_turn_id: str,
        user_confirmed: bool,
        kind: str = 'fact',
        expires_at: Optional[float] = None,
    ) -> MemoryMutationResult:
        """Validate evidence and create one durable memory."""
        replay = self._memory_store.prepare_confirmed_create(
            user_id=user_id,
            request_id=request_id,
            content=content,
            evidence_conversation_id=evidence_conversation_id,
            evidence_turn_id=evidence_turn_id,
            user_confirmed=user_confirmed,
            kind=kind,
            expires_at=expires_at,
        )
        if replay.cached_result is not None:
            return self._validated_cached_result(
                replay.cached_result,
                evidence_conversation_id,
                evidence_turn_id,
            )
        evidence = self._require_evidence(
            user_id,
            evidence_conversation_id,
            evidence_turn_id,
            user_confirmed,
        )
        return self._memory_store.commit_confirmed(
            user_id=user_id,
            request_id=request_id,
            content=content,
            evidence_conversation_id=evidence_conversation_id,
            evidence_turn_id=evidence_turn_id,
            user_confirmed=True,
            kind=kind,
            expires_at=expires_at,
            **self._provenance_arguments(evidence),
        )

    def update_confirmed(
        self,
        user_id: str,
        memory_id: str,
        request_id: str,
        expected_revision: int,
        content: str,
        evidence_conversation_id: str,
        evidence_turn_id: str,
        user_confirmed: bool,
        kind: Optional[str] = None,
        expires_at: Any = _UNSET,
    ) -> MemoryMutationResult:
        """Validate evidence and update one owned memory with CAS."""
        prepare_arguments = {
            'user_id': user_id,
            'memory_id': memory_id,
            'request_id': request_id,
            'expected_revision': expected_revision,
            'content': content,
            'evidence_conversation_id': evidence_conversation_id,
            'evidence_turn_id': evidence_turn_id,
            'user_confirmed': user_confirmed,
            'kind': kind,
        }
        if expires_at is not _UNSET:
            prepare_arguments['expires_at'] = expires_at
        replay = self._memory_store.prepare_confirmed_update(
            **prepare_arguments
        )
        if replay.cached_result is not None:
            return self._validated_cached_result(
                replay.cached_result,
                evidence_conversation_id,
                evidence_turn_id,
            )
        evidence = self._require_evidence(
            user_id,
            evidence_conversation_id,
            evidence_turn_id,
            user_confirmed,
        )
        arguments = {
            'user_id': user_id,
            'memory_id': memory_id,
            'request_id': request_id,
            'expected_revision': expected_revision,
            'content': content,
            'evidence_conversation_id': evidence_conversation_id,
            'evidence_turn_id': evidence_turn_id,
            'user_confirmed': True,
            'kind': kind,
        }
        if expires_at is not _UNSET:
            arguments['expires_at'] = expires_at
        arguments.update(self._provenance_arguments(evidence))
        return self._memory_store.update_confirmed(**arguments)

    def delete_confirmed(
        self,
        user_id: str,
        memory_id: str,
        request_id: str,
        expected_revision: int,
        evidence_conversation_id: str,
        evidence_turn_id: str,
        user_confirmed: bool,
    ) -> MemoryMutationResult:
        """Validate evidence and delete one owned memory with CAS."""
        replay = self._memory_store.prepare_confirmed_delete(
            user_id=user_id,
            memory_id=memory_id,
            request_id=request_id,
            expected_revision=expected_revision,
            evidence_conversation_id=evidence_conversation_id,
            evidence_turn_id=evidence_turn_id,
            user_confirmed=user_confirmed,
        )
        if replay.cached_result is not None:
            return self._validated_cached_result(
                replay.cached_result,
                evidence_conversation_id,
                evidence_turn_id,
            )
        evidence = self._require_evidence(
            user_id,
            evidence_conversation_id,
            evidence_turn_id,
            user_confirmed,
        )
        return self._memory_store.delete_confirmed(
            user_id=user_id,
            memory_id=memory_id,
            request_id=request_id,
            expected_revision=expected_revision,
            evidence_conversation_id=evidence_conversation_id,
            evidence_turn_id=evidence_turn_id,
            user_confirmed=True,
            **self._provenance_arguments(evidence),
        )

    @staticmethod
    def _provenance_arguments(
        evidence: CompletedTurnEvidence,
    ) -> dict[str, Any]:
        """Bind the validator's exact turn provenance to the mutation."""
        return {
            'evidence_session_instance_id': evidence.session_instance_id,
            'evidence_generation': evidence.generation,
            'evidence_completed_at': evidence.completed_at,
        }

    @staticmethod
    def _validated_cached_result(
        result: MemoryMutationResult,
        conversation_id: str,
        turn_id: str,
    ) -> MemoryMutationResult:
        """Replay only a result previously bound to validated evidence."""
        normalized_conversation = validate_conversation_id(conversation_id)
        normalized_turn = validate_turn_id(turn_id)
        if (
            result.evidence_conversation_id != normalized_conversation
            or result.evidence_turn_id != normalized_turn
            or not result.evidence_session_instance_id
            or result.evidence_generation is None
            or result.evidence_completed_at is None
        ):
            raise MemoryEvidenceError(
                'cached mutation has no validated evidence provenance'
            )
        return result

    def _require_evidence(
        self,
        user_id: str,
        conversation_id: str,
        turn_id: str,
        user_confirmed: Any,
    ) -> CompletedTurnEvidence:
        """Validate consent first, then verify the completed turn binding."""
        if user_confirmed is not True:
            raise MemoryConsentError(
                'memory mutation requires explicit user confirmation'
            )
        normalized_user = validate_user_id(user_id)
        normalized_conversation = validate_conversation_id(
            conversation_id
        )
        normalized_turn = validate_turn_id(turn_id)
        evidence = self._evidence_validator.validate_completed_turn(
            normalized_user,
            normalized_conversation,
            normalized_turn,
        )
        if not isinstance(evidence, CompletedTurnEvidence):
            raise MemoryEvidenceError(
                'evidence validator returned an invalid result'
            )
        if (
            evidence.user_id != normalized_user
            or evidence.conversation_id != normalized_conversation
            or evidence.turn_id != normalized_turn
        ):
            raise MemoryEvidenceError(
                'completed confirmation turn binding did not match'
            )
        if not evidence.session_instance_id:
            raise MemoryEvidenceError(
                'completed confirmation turn evidence was incomplete'
            )
        if (
            isinstance(evidence.generation, bool)
            or not isinstance(evidence.generation, int)
            or evidence.generation < 1
        ):
            raise MemoryEvidenceError(
                'completed confirmation turn evidence was incomplete'
            )
        if (
            isinstance(evidence.completed_at, bool)
            or not isinstance(evidence.completed_at, (int, float))
            or not math.isfinite(float(evidence.completed_at))
        ):
            raise MemoryEvidenceError(
                'completed confirmation turn evidence was incomplete'
            )
        return evidence
