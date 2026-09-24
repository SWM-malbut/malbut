"""Bounded situation confirmation, independent of ROS and audio timing."""

from dataclasses import dataclass
from typing import Optional, Protocol

from malbut_agent_server.providers.base import ProviderError
from malbut_agent_server.schemas import MAX_SPEECH_TRANSCRIPT_LENGTH


ASSESSMENTS = ('confirmed_incident', 'resolved', 'unknown')
MAX_SITUATION_SUMMARY_LENGTH = 4000
MAX_REQUEST_ID_LENGTH = 200
MAX_SITUATION_TYPE_LENGTH = 100
MAX_QUESTION_LENGTH = 600
MAX_CLARIFICATIONS = 2


def _text(value, name, limit, *, allow_empty=False):
    if (not isinstance(value, str) or len(value) > limit
            or (not allow_empty and not value.strip())):
        raise ValueError(f'{name} must be a bounded string')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError as error:
        raise ValueError(f'{name} must be valid UTF-8') from error
    return value


@dataclass(frozen=True)
class SituationRequest:
    """Manager's text-only description of the situation to check."""

    request_id: str
    situation_type: str
    summary: str

    def __post_init__(self):
        _text(self.request_id, 'request_id', MAX_REQUEST_ID_LENGTH)
        _text(self.situation_type, 'situation_type', MAX_SITUATION_TYPE_LENGTH)
        _text(self.summary, 'summary', MAX_SITUATION_SUMMARY_LENGTH)


@dataclass(frozen=True)
class SituationResult:
    """The two final fields visible to Manager."""

    situation_assessment: str
    help_needed: bool

    def __post_init__(self):
        if self.situation_assessment not in ASSESSMENTS:
            raise ValueError('unsupported situation assessment')
        if type(self.help_needed) is not bool:
            raise ValueError('help_needed must be a boolean')


@dataclass(frozen=True)
class SituationTurn:
    """A question or closing sentence; only closing turns carry a result."""

    text: str
    result: Optional[SituationResult] = None


@dataclass(frozen=True)
class SituationExchange:
    """Completed question and answer, retained only for this incident."""

    question: str
    answer: str


@dataclass(frozen=True)
class SituationContext:
    """Read-only input to a semantic provider."""

    request: SituationRequest
    stage: str
    situation_assessment: str
    history: tuple[SituationExchange, ...]
    question: str = ''
    answer: Optional[str] = None


@dataclass(frozen=True)
class SituationInterpretation:
    """Latest cumulative judgment, with no authority over retry limits.

    ``unknown`` is a real replacement judgment, not a leave-unchanged sentinel.
    Providers preserve established facts when the answer does not retract them.
    """

    situation_assessment: str
    help_needed: Optional[bool]
    question: str

    def __post_init__(self):
        if self.situation_assessment not in ASSESSMENTS:
            raise ValueError('unsupported situation assessment')
        if self.help_needed is not None and type(self.help_needed) is not bool:
            raise ValueError('help_needed must be a boolean or null')
        _text(self.question, 'question', MAX_QUESTION_LENGTH, allow_empty=True)


class SituationProvider(Protocol):
    """Generate a first question or interpret an answer in its context."""

    def evaluate(self, context: SituationContext) -> SituationInterpretation:
        """Return semantic judgments or raise on operational failure."""
        ...


class SituationDialogue:
    """Serial conversation state; the caller owns scheduling and cancellation.

    Provider failures propagate to the caller and never become user silence.
    The caller invokes ``no_response`` only after successful question playback
    and its answer-start deadline. This class contains no clocks or audio I/O.
    """

    def __init__(self, provider: SituationProvider):
        self.provider = provider
        self.request = None
        self.stage = 'situation'
        self.situation_assessment = 'unknown'
        self._history = []
        self._question = ''
        self._clarifications = {'situation': 0, 'help': 0}
        self._asked_stages = {'situation'}
        self._last_turn = None

    @property
    def result(self):
        """Expose the completed result without publishing progress."""
        return self._last_turn.result if self._last_turn else None

    def start(self, request_id, situation_type, summary):
        """Start one confirmation; reject replacing an active conversation."""
        if self.request is not None and self.result is None:
            raise RuntimeError('situation dialogue is already active')
        request = SituationRequest(request_id, situation_type, summary)
        context = SituationContext(request, 'situation', 'unknown', ())
        interpreted = self._evaluate(context)
        self._require_question(interpreted)
        # An incoming summary describes a situation requiring confirmation.
        # The first generated question is not evidence of an actual incident.
        self.request = request
        self.stage = 'situation'
        self.situation_assessment = 'unknown'
        self._history = []
        self._clarifications = {'situation': 0, 'help': 0}
        self._asked_stages = {'situation'}
        self._question = interpreted.question
        self._last_turn = SituationTurn(self._question)
        return self._last_turn

    def answer(self, text):
        """Interpret an answer, asking only for still unresolved information."""
        self._require_started()
        if self.result is not None:
            return self._last_turn
        _text(text, 'answer', MAX_SPEECH_TRANSCRIPT_LENGTH)
        context = SituationContext(
            self.request, self.stage, self.situation_assessment,
            tuple(self._history), self._question, text,
        )
        interpreted = self._evaluate(context)
        # Providers return a cumulative snapshot. A user may explicitly retract
        # a previous confirmation, including changing it back to uncertainty.
        assessment = interpreted.situation_assessment

        # Explicit assistance requests/refusals can complete either phase.
        help_needed = interpreted.help_needed
        if help_needed is None and assessment == 'resolved':
            help_needed = False
        next_stage = self.stage
        clarifications = dict(self._clarifications)
        asked_stages = set(self._asked_stages)
        if help_needed is None:
            next_stage = 'help' if assessment == 'confirmed_incident' else 'situation'
            if next_stage not in asked_stages:
                asked_stages.add(next_stage)
            elif clarifications[next_stage] >= MAX_CLARIFICATIONS:
                help_needed = True
            else:
                clarifications[next_stage] += 1
            if help_needed is None:
                self._require_question(interpreted)

        # Commit only after the entire provider response has been validated.
        self._history.append(SituationExchange(self._question, text))
        self.situation_assessment = assessment
        self.stage = next_stage
        self._clarifications = clarifications
        self._asked_stages = asked_stages
        if help_needed is not None:
            reason = ('unclear' if interpreted.help_needed is None
                      and assessment != 'resolved' else 'answered')
            return self._finish(help_needed, reason)
        self._question = interpreted.question
        self._last_turn = SituationTurn(self._question)
        return self._last_turn

    def no_response(self):
        """Escalate silence immediately while preserving established facts."""
        self._require_started()
        if self.result is not None:
            return self._last_turn
        return self._finish(True, 'no_response')

    def _evaluate(self, context):
        interpreted = self.provider.evaluate(context)
        if not isinstance(interpreted, SituationInterpretation):
            raise ProviderError('invalid situation provider response')
        return interpreted

    @staticmethod
    def _require_question(interpreted):
        if not interpreted.question.strip():
            raise ProviderError('situation provider omitted the next question')

    def _require_started(self):
        if self.request is None:
            raise RuntimeError('situation dialogue has not started')

    def _finish(self, help_needed, reason):
        if reason == 'no_response':
            closing = '답변을 확인하지 못했어요. 도움이 필요한 상황으로 판단할게요.'
        elif reason == 'unclear':
            closing = '상황을 충분히 확인하지 못했어요. 도움이 필요한 상황으로 판단할게요.'
        elif help_needed:
            closing = '도움이 필요하신 것으로 확인했어요.'
        elif self.situation_assessment == 'resolved':
            closing = '알겠어요. 상황을 확인했어요. 말씀해 주셔서 고마워요.'
        else:
            closing = '알겠어요. 도움이 필요하지 않으신 것으로 확인했어요.'
        self._last_turn = SituationTurn(closing, SituationResult(
            self.situation_assessment, help_needed,
        ))
        return self._last_turn


class MockSituationProvider:
    """Offline fixtures only; production uses a semantic model provider."""

    def evaluate(self, context):
        """Recognize exact demonstration answers without network access."""
        situation_question = ('혹시 넘어지신 건가요?'
                              if context.request.situation_type in ('fall', '낙상')
                              else '지금 어떤 상황인지 말씀해 주시겠어요?')
        help_question = '지금 괜찮으신가요? 도움이 필요하신가요?'
        if context.answer is None:
            return SituationInterpretation('unknown', None, situation_question)
        normalized = ''.join(context.answer.split()).rstrip('.!?。！？')
        established = context.situation_assessment
        if normalized in {
            '그냥누워있는거야', '그냥누워있었어', '그냥누워있어', '쉬고있어',
            '넘어진게아니야', '안넘어졌어',
        }:
            return SituationInterpretation('resolved', None, '')
        if normalized in {'넘어진건확실하지않아', '아까넘어졌다는말은취소할게'}:
            return SituationInterpretation('unknown', None, situation_question)
        if normalized in {'도와줘', '도와주세요', '도움이필요해', '도움이필요해요'}:
            return SituationInterpretation(established, True, '')
        if normalized in {
            '도움은필요없어', '도움이필요없어', '도움은필요없어요',
            '도움이필요없어요', '도움필요없어', '도움필요없어요',
            '도움필요없습니다', '도움필요하지않아', '도움필요하지않아요',
            '도움이필요하지않아요', '넘어졌지만도움은필요없어',
        }:
            assessment = ('confirmed_incident'
                          if normalized == '넘어졌지만도움은필요없어' else established)
            return SituationInterpretation(assessment, False, '')
        if normalized in {'넘어졌어', '넘어졌어요', '미끄러졌어', '넘어진건맞아'}:
            return SituationInterpretation('confirmed_incident', None, help_question)
        if (context.stage == 'situation'
                and context.request.situation_type in ('fall', '낙상')):
            if normalized in {'네', '응', '맞아', '맞아요'}:
                return SituationInterpretation('confirmed_incident', None, help_question)
            if normalized in {'아니요', '아니'}:
                return SituationInterpretation('resolved', None, '')
        if context.stage == 'help' and normalized in {'네', '응', '필요해요'}:
            return SituationInterpretation(established, True, '')
        if context.stage == 'help' and normalized in {'아니요', '아니', '괜찮아요', '괜찮아'}:
            return SituationInterpretation(established, False, '')
        return SituationInterpretation(
            established, None,
            situation_question if context.stage == 'situation' else help_question,
        )
