"""Incident facts, bounded clarification and assistance decisions."""

from dataclasses import asdict

import pytest

from malbut_agent_server.providers.base import ProviderError
from malbut_agent_server.situation_dialogue import (
    MockSituationProvider, SituationDialogue, SituationInterpretation,
    SituationRequest,
)


class ScriptedProvider:
    """Supply semantic judgments without an API call or keyword assumptions."""

    def __init__(self, *replies):
        self.replies = iter(replies)
        self.contexts = []

    def evaluate(self, context):
        self.contexts.append(context)
        value = next(self.replies)
        if isinstance(value, Exception):
            raise value
        return value


def judgment(assessment='unknown', help_needed=None, question='실제로 넘어지셨나요?'):
    return SituationInterpretation(assessment, help_needed, question)


def start(provider=None, summary='거실 바닥에 누워 있어 낙상이 의심됨'):
    dialogue = SituationDialogue(provider or MockSituationProvider())
    turn = dialogue.start(request_id='request-1', situation_type='fall', summary=summary)
    assert turn.result is None
    assert turn.text
    return dialogue


def test_resting_answer_resolves_conflicting_summary_without_asking_for_help():
    provider = ScriptedProvider(judgment(), judgment('resolved', None, ''))
    dialogue = start(provider, summary='VLM은 실제 낙상이 발생했다고 판단함')
    final = dialogue.answer('아니, 스트레칭하려고 일부러 누운 거야.')
    assert asdict(final.result) == {
        'situation_assessment': 'resolved', 'help_needed': False,
    }
    assert provider.contexts[-1].answer == '아니, 스트레칭하려고 일부러 누운 거야.'
    assert len(provider.contexts) == 2


@pytest.mark.parametrize('answer,expected', [
    ('도와주세요', True), ('도움은 필요 없어요', False),
])
def test_explicit_help_decision_can_finish_while_actual_event_is_unknown(answer, expected):
    dialogue = start()
    final = dialogue.answer(answer)
    assert final.result.situation_assessment == 'unknown'
    assert final.result.help_needed is expected
    assert '신고' not in final.text and '연락했' not in final.text


def test_confirm_incident_then_respect_no_help_answer():
    dialogue = start()
    followup = dialogue.answer('넘어졌어요')
    assert followup.result is None
    assert dialogue.stage == 'help'
    final = dialogue.answer('괜찮아요')
    assert final.result.situation_assessment == 'confirmed_incident'
    assert final.result.help_needed is False


def test_mock_short_answers_are_interpreted_against_the_current_question():
    dialogue = start()
    assert dialogue.answer('네').result is None
    final = dialogue.answer('아니요')
    assert final.result.situation_assessment == 'confirmed_incident'
    assert final.result.help_needed is False
    other = start()
    assert other.answer('아니요').result.situation_assessment == 'resolved'


@pytest.mark.parametrize('answer', [
    '도움 필요 없어', '도움 필요 없어요', '도움이 필요 없어요',
    '도움 필요 없습니다', '도움 필요하지 않아', '도움 필요하지 않아요',
])
def test_mock_recognizes_common_explicit_help_refusals_after_confirmed_fall(answer):
    dialogue = start()
    dialogue.answer('넘어졌어요')
    final = dialogue.answer(answer)
    assert final.result.situation_assessment == 'confirmed_incident'
    assert final.result.help_needed is False


def test_okay_alone_in_actual_event_phase_does_not_mean_just_resting():
    dialogue = start()
    turn = dialogue.answer('괜찮아')
    assert turn.result is None
    assert dialogue.situation_assessment == 'unknown'
    assert dialogue.stage == 'situation'
    assert dialogue.answer('그냥 누워 있는 거야').result.help_needed is False


def test_user_can_correct_an_earlier_incident_confirmation():
    dialogue = start()
    dialogue.answer('넘어졌어')
    final = dialogue.answer('넘어진 게 아니야')
    assert final.result.situation_assessment == 'resolved'
    assert final.result.help_needed is False


@pytest.mark.parametrize('confirmed', [False, True])
def test_no_response_requests_help_without_inventing_or_erasing_actual_event(confirmed):
    dialogue = start()
    if confirmed:
        dialogue.answer('넘어졌어요')
    final = dialogue.no_response()
    assert final.result.situation_assessment == (
        'confirmed_incident' if confirmed else 'unknown'
    )
    assert final.result.help_needed is True
    assert '답변을 확인하지 못했어요' in final.text


def test_silence_after_one_clarification_does_not_spend_remaining_retries():
    provider = ScriptedProvider(judgment(), judgment())
    dialogue = start(provider)
    dialogue.answer('음...')
    assert dialogue.no_response().result.help_needed is True
    assert len(provider.contexts) == 2


def test_only_two_clarifications_after_initial_ambiguous_answer():
    provider = ScriptedProvider(judgment(), judgment(), judgment(), judgment())
    dialogue = start(provider)
    assert dialogue.answer('글쎄').result is None
    assert dialogue.answer('그게...').result is None
    final = dialogue.answer('모르겠네')
    assert final.result.situation_assessment == 'unknown'
    assert final.result.help_needed is True
    assert len(provider.contexts) == 4


def test_each_confirmation_matter_has_its_own_two_clarifications():
    provider = ScriptedProvider(
        judgment(), judgment(), judgment(),
        judgment('confirmed_incident', None, '도움이 필요하세요?'),
        judgment('confirmed_incident', question='도움이 필요한지 말씀해 주시겠어요?'),
        judgment('confirmed_incident', question='지금 도움을 원하시나요?'),
        judgment('confirmed_incident'),
    )
    dialogue = start(provider)
    for answer in ['음', '어...', '넘어진 건 맞아', '음', '글쎄']:
        assert dialogue.answer(answer).result is None
    final = dialogue.answer('잘 모르겠어')
    assert final.result.situation_assessment == 'confirmed_incident'
    assert final.result.help_needed is True
    # The model sees the exact immediately preceding question and old facts.
    last_context = provider.contexts[-1]
    assert last_context.question == '지금 도움을 원하시나요?'
    assert last_context.situation_assessment == 'confirmed_incident'
    assert last_context.history[2].answer == '넘어진 건 맞아'


def test_help_request_takes_precedence_over_an_otherwise_resolved_incident():
    provider = ScriptedProvider(judgment(), judgment('resolved', True, ''))
    dialogue = start(provider)
    final = dialogue.answer('그냥 누웠는데 일어나는 건 도와줘')
    assert final.result.situation_assessment == 'resolved'
    assert final.result.help_needed is True


@pytest.mark.parametrize('help_needed', [False, True])
def test_user_can_retract_confirmed_incident_to_unknown_and_keep_explicit_help_choice(help_needed):
    provider = ScriptedProvider(
        judgment(), judgment('confirmed_incident', None, '도움이 필요하세요?'),
        judgment('unknown', help_needed, ''),
    )
    dialogue = start(provider)
    dialogue.answer('넘어졌어요')
    choice = '도와주세요' if help_needed else '도움은 필요 없어요'
    final = dialogue.answer(f'아까 넘어졌다고 했는데 확실하지 않아요. {choice}')
    assert final.result.situation_assessment == 'unknown'
    assert final.result.help_needed is help_needed


def test_ambiguous_help_answer_preserves_actual_event_without_retraction():
    dialogue = start()
    dialogue.answer('넘어졌어요')
    assert dialogue.answer('몰라').result is None
    assert dialogue.stage == 'help'
    assert dialogue.situation_assessment == 'confirmed_incident'
    assert dialogue.no_response().result.situation_assessment == 'confirmed_incident'


def test_mock_explicit_retraction_reopens_actual_situation_confirmation():
    dialogue = start()
    dialogue.answer('넘어졌어요')
    followup = dialogue.answer('넘어진 건 확실하지 않아')
    assert followup.result is None
    assert dialogue.stage == 'situation'
    assert dialogue.situation_assessment == 'unknown'
    assert dialogue.no_response().result.situation_assessment == 'unknown'


def test_returning_to_situation_after_retraction_does_not_reset_its_clarification_budget():
    provider = ScriptedProvider(
        judgment(), judgment(),
        judgment('confirmed_incident', None, '도움이 필요하세요?'),
        judgment('unknown', None, '실제로 넘어진 것인지 다시 말씀해 주시겠어요?'),
        judgment('confirmed_incident', None, '지금 도움이 필요하세요?'),
        judgment('unknown', None, '실제로 무슨 일이 있었는지 말씀해 주시겠어요?'),
    )
    dialogue = start(provider)
    assert dialogue.answer('글쎄요').result is None  # First situation clarification.
    assert dialogue.answer('넘어졌어요').result is None
    assert dialogue.stage == 'help'
    assert dialogue.answer('아까 대답이 틀렸을 수 있어요').result is None
    assert dialogue.stage == 'situation'  # Second situation clarification.
    assert dialogue.answer('생각해 보니 넘어졌어요').result is None
    assert dialogue.stage == 'help'
    final = dialogue.answer('아니, 다시 생각하니 확실하지 않아요')
    assert final.result.situation_assessment == 'unknown'
    assert final.result.help_needed is True


def test_returning_to_help_after_reconfirmation_does_not_reset_its_clarification_budget():
    provider = ScriptedProvider(
        judgment(), judgment('confirmed_incident', None, '도움이 필요하세요?'),
        judgment('confirmed_incident', None, '도움을 원하시는지 알려주시겠어요?'),
        judgment('unknown', None, '실제로 무슨 일이 있었는지 말씀해 주시겠어요?'),
        judgment('confirmed_incident', None, '지금 도움을 원하시나요?'),
        judgment('confirmed_incident', None, '다시 질문'),
    )
    dialogue = start(provider)
    for answer in ('넘어졌어', '글쎄', '아까 넘어졌다는 답은 확실하지 않아', '넘어진 게 맞아'):
        assert dialogue.answer(answer).result is None
    final = dialogue.answer('도움이 필요한지는 모르겠어')
    assert final.result.situation_assessment == 'confirmed_incident'
    assert final.result.help_needed is True


def test_completed_turns_are_idempotent_and_dialogue_can_start_fresh():
    provider = ScriptedProvider(judgment(), judgment('resolved', None, ''), judgment())
    dialogue = start(provider)
    final = dialogue.answer('일부러 누웠어')
    assert dialogue.answer('다시 보내진 답변') is final
    assert dialogue.no_response() is final
    new = dialogue.start('new-request', 'fall', '새 낙상 의심')
    assert new.result is None
    assert dialogue.situation_assessment == 'unknown'
    assert provider.contexts[-1].history == ()


def test_other_incident_type_uses_same_confirmation_and_help_flow():
    provider = ScriptedProvider(
        judgment(question='연기가 실제로 나고 있나요?'),
        judgment('confirmed_incident', None, '도움이 필요하신가요?'),
        judgment('confirmed_incident', True, ''),
    )
    dialogue = SituationDialogue(provider)
    dialogue.start('smoke-1', 'smoke', '주방에서 연기로 추정되는 현상 관찰')
    assert dialogue.answer('네, 연기가 나요').result is None
    final = dialogue.answer('네, 도와주세요')
    assert final.result.situation_assessment == 'confirmed_incident'
    assert final.result.help_needed is True


def test_provider_failure_is_operational_and_does_not_become_user_silence():
    provider = ScriptedProvider(judgment(), TimeoutError('provider unavailable'),
                                judgment('resolved', None, ''))
    dialogue = start(provider)
    with pytest.raises(TimeoutError):
        dialogue.answer('그냥 누워 있었어')
    assert dialogue.result is None
    assert dialogue.situation_assessment == 'unknown'
    assert dialogue.answer('그냥 누워 있었어').result.help_needed is False
    assert provider.contexts[-1].history == ()


def test_missing_followup_question_does_not_commit_partial_facts():
    provider = ScriptedProvider(judgment(), judgment('confirmed_incident', None, ''))
    dialogue = start(provider)
    with pytest.raises(ProviderError):
        dialogue.answer('넘어졌어')
    assert dialogue.situation_assessment == 'unknown'
    assert dialogue.result is None


def test_start_does_not_invent_a_confirmed_incident_from_a_model_guess():
    dialogue = start(ScriptedProvider(judgment('confirmed_incident', True)))
    assert dialogue.no_response().result.situation_assessment == 'unknown'


@pytest.mark.parametrize('value', ['', ' ', None, '가' * 16001, '\ud800'])
def test_unusable_answers_are_not_silence_or_a_model_call(value):
    provider = ScriptedProvider(judgment())
    dialogue = start(provider)
    with pytest.raises(ValueError):
        dialogue.answer(value)
    assert len(provider.contexts) == 1
    assert dialogue.result is None


@pytest.mark.parametrize('field,value', [
    ('request_id', ''), ('request_id', 'x' * 201),
    ('situation_type', ''), ('situation_type', 'x' * 101),
    ('summary', ''), ('summary', 'x' * 4001), ('summary', '\ud800'),
])
def test_request_bounds(field, value):
    fields = {'request_id': 'request-1', 'situation_type': 'fall', 'summary': '요약'}
    fields[field] = value
    with pytest.raises(ValueError):
        SituationRequest(**fields)


def test_an_active_situation_cannot_be_overwritten_by_a_new_request():
    dialogue = start()
    with pytest.raises(RuntimeError):
        dialogue.start('new-request', 'fall', '다른 상황')
    assert dialogue.request.request_id == 'request-1'
