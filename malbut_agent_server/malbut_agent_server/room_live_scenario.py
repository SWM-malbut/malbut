"""Bind one continuous voice lifecycle to the simulated room mission.

This bridge performs no ROS, camera, network, or filesystem work. A voice
confirmation request is display evidence only: execution still requires a
separately issued ``TrustedConfirmation`` resolved inside
``RoomMonitoringMission``.
"""

from __future__ import annotations

import threading
from typing import Optional

from malbut_agent_server.continuous_voice import (
    AWAITING_CONFIRMATION,
    MISSION_WAIT,
    ContinuousVoiceSession,
    ToolConfirmationRequest,
    VoiceCycleResult,
)
from malbut_agent_server.orchestrator import OrchestrationResult
from malbut_agent_server.room_mission import (
    MissionFeedback,
    MissionProposalHandle,
    ProposalResult,
    RoomMonitoringMission,
    monitor_room_arguments_digest,
)


class RoomLiveScenarioValidationError(ValueError):
    """Report an invalid cross-layer handoff without reflecting content."""


class RoomLiveScenarioCoordinator:
    """Keep voice and simulation mission states in one strict lifecycle."""

    def __init__(
        self,
        voice: ContinuousVoiceSession,
        mission: RoomMonitoringMission,
    ) -> None:
        """Snapshot the exact already-configured trusted boundaries."""
        if type(voice) is not ContinuousVoiceSession:
            raise TypeError('voice must be a ContinuousVoiceSession')
        if type(mission) is not RoomMonitoringMission:
            raise TypeError('mission must be a RoomMonitoringMission')
        self._voice = voice
        self._mission = mission
        self._lock = threading.RLock()
        self._pending_request: Optional[ToolConfirmationRequest] = None
        self._pending_proposal: Optional[MissionProposalHandle] = None
        self._active_request: Optional[ToolConfirmationRequest] = None
        self._active_proposal: Optional[MissionProposalHandle] = None
        self._active_tool_call_id: Optional[str] = None
        self._pending_transition: Optional[object] = None

    def propose_from_voice(
        self,
        cycle: VoiceCycleResult,
    ) -> ProposalResult:
        """Bind the exact pending voice request to one opaque proposal."""
        self._discard_stale_pending()
        token = object()
        with self._lock:
            if (
                self._pending_transition is not None
                or self._pending_request is not None
                or self._active_request is not None
            ):
                raise RoomLiveScenarioValidationError(
                    'room scenario is already active'
                )
            self._pending_transition = token
        request = None
        validation_failure = None
        proposal_failed = False
        try:
            request, result = self._bound_result(cycle)
            proposed = self._mission.propose(result)
        except RoomLiveScenarioValidationError as error:
            validation_failure = error
        except Exception:
            proposal_failed = True
        if validation_failure is not None:
            self._clear_transition(token)
            raise validation_failure
        if proposal_failed:
            if (
                request is not None
                and self._voice.pending_confirmation_request is request
            ):
                self._voice.complete_mission(
                    request,
                    outcome='cancelled',
                )
            self._clear_transition(token)
            raise RoomLiveScenarioValidationError(
                'room mission proposal failed'
            )
        if proposed.proposal is None:
            self._voice.complete_mission(
                request,
                outcome='cancelled',
            )
            self._clear_transition(token)
            return proposed
        if not self._voice_pending_is(request):
            try:
                self._mission.deny(proposed.proposal)
            except Exception:
                pass
            self._clear_transition(token)
            raise RoomLiveScenarioValidationError(
                'voice proposal binding is invalid'
            )
        with self._lock:
            conflicted = (
                self._pending_transition is not token
                or self._pending_request is not None
                or self._active_request is not None
            )
            if not conflicted:
                self._pending_request = request
                self._pending_proposal = proposed.proposal
                self._pending_transition = None
        if conflicted:
            try:
                self._mission.deny(proposed.proposal)
            except Exception:
                pass
            self._clear_transition(token)
            raise RoomLiveScenarioValidationError(
                'room scenario transition conflicted'
            )
        return proposed

    def confirm(
        self,
        proposal: MissionProposalHandle,
        confirmation_id: str,
    ) -> MissionFeedback:
        """Confirm in the mission first, then enter voice mission-wait."""
        token = object()
        with self._lock:
            if self._pending_transition is not None:
                raise RoomLiveScenarioValidationError(
                    'room scenario transition is in progress'
                )
            request = self._require_pending_locked(proposal)
            self._pending_transition = token
        if not self._voice_pending_is(request):
            try:
                self._mission.deny(proposal)
            except Exception:
                pass
            self._clear_pending_transition(token, proposal)
            raise RoomLiveScenarioValidationError(
                'pending room proposal binding is invalid'
            )
        confirmation_failed = False
        try:
            feedback = self._mission.confirm(proposal, confirmation_id)
        except Exception:
            confirmation_failed = True
        if confirmation_failed:
            self._voice.complete_mission(
                request,
                outcome='cancelled',
            )
            self._clear_pending_transition(token, proposal)
            raise RoomLiveScenarioValidationError(
                'room mission confirmation failed'
            )
        if feedback.status != 'confirmed':
            if self._pending_feedback_is_terminal(feedback):
                self._voice.complete_mission(
                    request,
                    outcome='cancelled',
                )
                self._clear_pending_transition(token, proposal)
            else:
                self._clear_transition(token)
            return feedback
        if feedback.tool_call_id is None:
            self._voice.complete_mission(
                request,
                outcome='cancelled',
            )
            self._clear_pending_transition(token, proposal)
            raise RoomLiveScenarioValidationError(
                'confirmed mission has no Tool call ID'
            )
        try:
            accepted = self._voice.accept_confirmation(request)
        except Exception:
            accepted = None
        if (
            accepted is None
            or accepted.status != 'ready'
            or accepted.code != 'mission_wait'
        ):
            try:
                cancelled = self._mission.cancel(
                    feedback.tool_call_id,
                    proposal,
                )
            except Exception:
                cancelled = feedback
            if self._voice.pending_confirmation_request is request:
                self._voice.complete_mission(
                    request,
                    outcome='cancelled',
                )
            self._clear_pending_transition(token, proposal)
            return cancelled
        with self._lock:
            if (
                self._pending_transition is not token
                or self._pending_request is not request
                or self._pending_proposal is not proposal
            ):
                conflicted = True
            else:
                conflicted = False
                self._pending_request = None
                self._pending_proposal = None
                self._active_request = request
                self._active_proposal = proposal
                self._active_tool_call_id = feedback.tool_call_id
                self._pending_transition = None
        if conflicted:
            try:
                self._mission.cancel(
                    feedback.tool_call_id,
                    proposal,
                )
            except Exception:
                pass
            if self._voice.active_mission_request is request:
                self._voice.complete_mission(
                    request,
                    outcome='cancelled',
                )
            self._clear_transition(token)
            raise RoomLiveScenarioValidationError(
                'room scenario transition conflicted'
            )
        return feedback

    def confirm_and_execute(
        self,
        proposal: MissionProposalHandle,
        confirmation_id: str,
    ) -> MissionFeedback:
        """Confirm and synchronously run one bounded simulated mission."""
        confirmation = self.confirm(proposal, confirmation_id)
        if confirmation.status != 'confirmed':
            return confirmation
        return self.execute(confirmation.tool_call_id, proposal)

    def execute(
        self,
        tool_call_id: str,
        proposal: MissionProposalHandle,
    ) -> MissionFeedback:
        """Execute only the active Tool ID and terminally rearm voice."""
        with self._lock:
            request = self._require_active_locked(tool_call_id, proposal)
        if not self._voice_active_is(request):
            raise RoomLiveScenarioValidationError(
                'active room mission binding is invalid'
            )
        execution_failed = False
        try:
            feedback = self._mission.execute(tool_call_id, proposal)
        except Exception:
            execution_failed = True
        if execution_failed:
            self._fail_active(request, proposal, tool_call_id)
            raise RoomLiveScenarioValidationError(
                'room mission execution failed'
            )
        self._complete_active(request, proposal, tool_call_id, feedback)
        return feedback

    def deny(
        self,
        proposal: MissionProposalHandle,
    ) -> MissionFeedback:
        """Deny one pending proposal and resume wake-word listening."""
        token = object()
        with self._lock:
            if self._pending_transition is not None:
                raise RoomLiveScenarioValidationError(
                    'room scenario transition is in progress'
                )
            request = self._require_pending_locked(proposal)
            self._pending_transition = token
        if not self._voice_pending_is(request):
            try:
                self._mission.deny(proposal)
            except Exception:
                pass
            self._clear_pending_transition(token, proposal)
            raise RoomLiveScenarioValidationError(
                'pending room proposal binding is invalid'
            )
        denial_failed = False
        try:
            feedback = self._mission.deny(proposal)
        except Exception:
            denial_failed = True
        if denial_failed:
            self._voice.complete_mission(
                request,
                outcome='cancelled',
            )
            self._clear_pending_transition(token, proposal)
            raise RoomLiveScenarioValidationError(
                'room mission denial failed'
            )
        if feedback.status == 'cancelled':
            self._voice.complete_mission(
                request,
                outcome='denied',
            )
            self._clear_pending_transition(token, proposal)
        elif self._pending_feedback_is_terminal(feedback):
            self._voice.complete_mission(
                request,
                outcome='cancelled',
            )
            self._clear_pending_transition(token, proposal)
        else:
            self._clear_transition(token)
        return feedback

    def cancel(
        self,
        tool_call_id: str,
        proposal: MissionProposalHandle,
    ) -> MissionFeedback:
        """Cancel one active mission and terminally rearm voice."""
        with self._lock:
            request = self._require_active_locked(tool_call_id, proposal)
        if not self._voice_active_is(request):
            raise RoomLiveScenarioValidationError(
                'active room mission binding is invalid'
            )
        cancellation_failed = False
        try:
            feedback = self._mission.cancel(tool_call_id, proposal)
        except Exception:
            cancellation_failed = True
        if cancellation_failed:
            self._fail_active(request, proposal, tool_call_id)
            raise RoomLiveScenarioValidationError(
                'room mission cancellation failed'
            )
        if (
            feedback.status in {'cancelled', 'failed', 'timed_out'}
            or feedback.code == 'authority_revoked'
        ):
            self._complete_active(
                request,
                proposal,
                tool_call_id,
                feedback,
            )
        return feedback

    def _require_pending_locked(
        self,
        proposal: MissionProposalHandle,
    ) -> ToolConfirmationRequest:
        request = self._pending_request
        if (
            request is None
            or self._pending_proposal is not proposal
        ):
            raise RoomLiveScenarioValidationError(
                'pending room proposal binding is invalid'
            )
        return request

    def _require_active_locked(
        self,
        tool_call_id: str,
        proposal: MissionProposalHandle,
    ) -> ToolConfirmationRequest:
        request = self._active_request
        if (
            request is None
            or self._active_proposal is not proposal
            or self._active_tool_call_id != tool_call_id
        ):
            raise RoomLiveScenarioValidationError(
                'active room mission binding is invalid'
            )
        return request

    def _voice_pending_is(
        self,
        request: ToolConfirmationRequest,
    ) -> bool:
        return (
            self._voice.state == AWAITING_CONFIRMATION
            and self._voice.pending_confirmation_request is request
        )

    def _voice_active_is(
        self,
        request: ToolConfirmationRequest,
    ) -> bool:
        return (
            self._voice.state == MISSION_WAIT
            and self._voice.active_mission_request is request
        )

    def _complete_active(
        self,
        request: ToolConfirmationRequest,
        proposal: MissionProposalHandle,
        tool_call_id: str,
        feedback: MissionFeedback,
    ) -> None:
        if feedback.status not in {
            'succeeded',
            'failed',
            'cancelled',
            'timed_out',
        } and feedback.code != 'authority_revoked':
            return
        outcome = 'failed'
        if feedback.status == 'succeeded':
            outcome = 'succeeded'
        elif feedback.status == 'cancelled':
            outcome = 'cancelled'
        terminal = self._voice.complete_mission(
            request,
            outcome=outcome,
        )
        with self._lock:
            if (
                self._active_request is request
                and self._active_proposal is proposal
                and self._active_tool_call_id == tool_call_id
                and terminal.code in {
                    f'mission_{outcome}',
                    'mission_terminal_replay',
                }
            ):
                self._active_request = None
                self._active_proposal = None
                self._active_tool_call_id = None

    def _fail_active(
        self,
        request: ToolConfirmationRequest,
        proposal: MissionProposalHandle,
        tool_call_id: str,
    ) -> None:
        self._voice.complete_mission(request, outcome='failed')
        with self._lock:
            if (
                self._active_request is request
                and self._active_proposal is proposal
                and self._active_tool_call_id == tool_call_id
            ):
                self._active_request = None
                self._active_proposal = None
                self._active_tool_call_id = None

    @staticmethod
    def _pending_feedback_is_terminal(
        feedback: MissionFeedback,
    ) -> bool:
        return (
            feedback.status in {'failed', 'cancelled', 'timed_out'}
            or feedback.code in {
                'authority_revoked',
                'confirmation_replay',
            }
        )

    def _clear_pending_locked(self) -> None:
        self._pending_request = None
        self._pending_proposal = None

    def _discard_stale_pending(self) -> None:
        """Reconcile a voice-side expiry before accepting a new cycle."""
        token = object()
        with self._lock:
            if (
                self._pending_transition is not None
                or self._active_request is not None
                or self._pending_request is None
                or self._pending_proposal is None
            ):
                return
            request = self._pending_request
            proposal = self._pending_proposal
            self._pending_transition = token
        if self._voice_pending_is(request):
            self._clear_transition(token)
            return
        try:
            self._mission.deny(proposal)
        except Exception:
            pass
        self._clear_pending_transition(token, proposal)

    def _clear_transition(self, token: object) -> None:
        with self._lock:
            if self._pending_transition is token:
                self._pending_transition = None

    def _clear_pending_transition(
        self,
        token: object,
        proposal: MissionProposalHandle,
    ) -> None:
        with self._lock:
            if (
                self._pending_transition is token
                and self._pending_proposal is proposal
            ):
                self._clear_pending_locked()
                self._pending_transition = None

    def _bound_result(
        self,
        cycle: VoiceCycleResult,
    ) -> tuple[ToolConfirmationRequest, OrchestrationResult]:
        """Return an exact, pending, non-authorizing monitor handoff."""
        if type(cycle) is not VoiceCycleResult:
            raise RoomLiveScenarioValidationError(
                'voice proposal binding is invalid'
            )
        request = cycle.confirmation_request
        pipeline = cycle.pipeline_result
        if (
            cycle.status != 'confirmation_required'
            or cycle.code != 'confirmation_required'
            or cycle.state != AWAITING_CONFIRMATION
            or type(request) is not ToolConfirmationRequest
            or self._voice.state != AWAITING_CONFIRMATION
            or self._voice.pending_confirmation_request is not request
            or pipeline is None
            or pipeline.status != 'responded'
            or pipeline.code != 'final_transcript_processed'
            or type(pipeline.agent_result) is not OrchestrationResult
        ):
            raise RoomLiveScenarioValidationError(
                'voice proposal binding is invalid'
            )
        result = pipeline.agent_result
        decision = result.decision
        digest_failed = False
        try:
            decision_digest = monitor_room_arguments_digest(
                decision.arguments
            )
        except (TypeError, ValueError):
            digest_failed = True
        if digest_failed:
            raise RoomLiveScenarioValidationError(
                'voice proposal binding is invalid'
            )
        binding_matches = (
            request.tool_name == 'monitor_room'
            and decision.type == 'tool_call'
            and decision.tool_name == 'monitor_room'
            and request.decision_id == result.decision_id
            and request.conversation_id == result.conversation_id
            and request.turn_id == result.turn_id
            and pipeline.request_id == result.request_id
            and pipeline.turn_id == result.turn_id
            and request.arguments_dict() == decision.arguments
            and request.arguments_digest == decision_digest
            and request.issued_at == result.issued_at
            and request.expires_at == result.expires_at
        )
        if not binding_matches:
            raise RoomLiveScenarioValidationError(
                'voice proposal binding is invalid'
            )
        return request, result


__all__ = [
    'RoomLiveScenarioCoordinator',
    'RoomLiveScenarioValidationError',
]
