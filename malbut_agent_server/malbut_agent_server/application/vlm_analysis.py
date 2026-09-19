"""Fail-safe fusion of local RGB-D evidence and replaceable VLM output."""

from typing import List, Mapping, Optional

from malbut_agent_server.domain.vlm import (
    FallAssessment,
    FallVerificationDecision,
    RecoveryState,
    VlmAnalysisRequest,
    VlmProviderResult,
)
from malbut_agent_server.ports.vlm_provider import (
    VlmProvider,
    VlmProviderError,
)
from malbut_agent_server.vlm_eval_schema import (
    VlmPrediction,
    validate_prediction,
)


class FallVlmAnalysisService:
    """Invoke one adapter and apply local evidence as a safety boundary."""

    def __init__(self, provider: VlmProvider) -> None:
        self._provider = provider

    def analyze(self, request: VlmAnalysisRequest) -> FallVerificationDecision:
        """Return UNOBSERVABLE on any provider or contract failure."""
        try:
            provider_result = self._provider.analyze(request)
            if not isinstance(provider_result.prediction, Mapping):
                return self._unobservable(
                    request,
                    provider_result=provider_result,
                    reason='invalid_provider_output',
                )
            prediction, schema_errors, semantic_errors = validate_prediction(
                provider_result.prediction,
                request.duration_s,
            )
            if prediction is None or schema_errors or semantic_errors:
                return self._unobservable(
                    request,
                    provider_result=provider_result,
                    reason='invalid_provider_output',
                )
        except (VlmProviderError, TimeoutError, ConnectionError, OSError):
            return self._unobservable(
                request,
                provider_result=None,
                reason='provider_unavailable',
            )
        except Exception:
            # A provider adapter is an external contract boundary.  Malformed
            # SDK responses and unexpected response shapes must not terminate
            # an incident worker or silently clear a safety candidate.
            return self._unobservable(
                request,
                provider_result=None,
                reason='provider_contract_failure',
            )
        return self._resolve(request, provider_result, prediction)

    def _resolve(
        self,
        request: VlmAnalysisRequest,
        provider_result: VlmProviderResult,
        prediction: VlmPrediction,
    ) -> FallVerificationDecision:
        evidence = request.evidence
        assessment = FallAssessment(prediction.fall_assessment)
        reasons: List[str] = []

        if (
            evidence.unresponsive_down_candidate
            and evidence.candidate_kind.value == 'found_down_candidate'
        ):
            assessment = FallAssessment.FOUND_DOWN
            reasons.append('unresponsive_found_down_cannot_be_cleared')
        elif (
            evidence.unresponsive_down_candidate
            and assessment is FallAssessment.NORMAL_ACTIVITY
        ):
            assessment = FallAssessment.UNOBSERVABLE
            reasons.append('unresponsive_fall_candidate_cannot_be_cleared')
        elif assessment is FallAssessment.UNOBSERVABLE:
            reasons.append('model_reported_unobservable')
        elif (
            not evidence.observation_usable
            and evidence.needs_safety_verification
        ):
            assessment = FallAssessment.UNOBSERVABLE
            reasons.append('local_observation_insufficient')
        elif (
            assessment is FallAssessment.NORMAL_ACTIVITY
            and (evidence.strong_observed_fall or evidence.strong_found_down)
        ):
            assessment = FallAssessment.UNOBSERVABLE
            reasons.append('model_cannot_clear_strong_local_evidence')
        elif (
            evidence.candidate_kind.value == 'found_down_candidate'
            and assessment is FallAssessment.CONFIRMED_FALL
        ):
            assessment = FallAssessment.FOUND_DOWN
            reasons.append('descent_not_observed')
        elif (
            evidence.strong_observed_fall
            and assessment is FallAssessment.FOUND_DOWN
        ):
            assessment = FallAssessment.CONFIRMED_FALL
            reasons.append('local_descent_and_floor_evidence_confirmed')

        recovery = RecoveryState(prediction.fall_recovery)
        if evidence.recovery is not RecoveryState.UNKNOWN:
            recovery = evidence.recovery
            reasons.append('local_recovery_state_preferred')

        risk = self._risk(assessment, recovery)
        return FallVerificationDecision(
            assessment=assessment,
            recovery=recovery,
            risk=risk,
            confidence=prediction.fall_confidence,
            provider=provider_result.provider,
            model_id=provider_result.model_id,
            explanation_ko=prediction.explanation_ko,
            evidence_ko=prediction.evidence_ko,
            policy_reasons=tuple(reasons),
            notify_guardian=(
                assessment
                in {FallAssessment.CONFIRMED_FALL, FallAssessment.FOUND_DOWN}
                or (
                    assessment is FallAssessment.UNOBSERVABLE
                    and evidence.needs_safety_verification
                )
            ),
        )

    @staticmethod
    def _risk(
        assessment: FallAssessment,
        recovery: RecoveryState,
    ) -> str:
        if assessment is FallAssessment.CONFIRMED_FALL:
            return (
                'attention'
                if recovery is RecoveryState.RECOVERED
                else 'urgent'
            )
        if assessment is FallAssessment.FOUND_DOWN:
            return 'urgent'
        if assessment is FallAssessment.UNOBSERVABLE:
            return 'attention'
        return 'none'

    @staticmethod
    def _unobservable(
        request: VlmAnalysisRequest,
        *,
        provider_result: Optional[VlmProviderResult],
        reason: str,
    ) -> FallVerificationDecision:
        return FallVerificationDecision(
            assessment=FallAssessment.UNOBSERVABLE,
            recovery=RecoveryState.UNKNOWN,
            risk='attention',
            confidence=0.0,
            provider=(
                provider_result.provider if provider_result else 'unavailable'
            ),
            model_id=(
                provider_result.model_id if provider_result else 'unavailable'
            ),
            explanation_ko='영상만으로 안전하게 확인할 수 없습니다.',
            evidence_ko=(),
            policy_reasons=(reason,),
            notify_guardian=request.evidence.needs_safety_verification,
        )
