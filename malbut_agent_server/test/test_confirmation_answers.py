"""An unchanged Action result must not erase the original answer provenance."""

import pytest

from malbut_agent_server.confirmation_answers import ConfirmationAnswers
from malbut_agent_server.situation_dialogue import SituationResult


def test_identical_wire_results_preserve_distinct_answers_by_request():
    receipts = ConfirmationAnswers()
    outcome = SituationResult('unknown', True)
    receipts.remember('silent', outcome, no_response=True)
    receipts.remember('help', outcome, no_response=False)
    result = dict(situation_assessment='unknown', help_needed=True)
    assert receipts.resolve('silent', result) == 'no_response'
    assert receipts.resolve('help', result) == 'help'
    receipts.remember('silent', outcome, no_response=True)  # Action replay.
    assert receipts.resolve('silent', result) == 'no_response'
    with pytest.raises(ValueError, match='conflicting'):
        receipts.remember('silent', outcome, no_response=False)


def test_missing_mismatched_or_evicted_receipt_cannot_invent_an_answer():
    receipts = ConfirmationAnswers(limit=1)
    receipts.remember('old', SituationResult('unknown', True), no_response=True)
    receipts.remember('new', SituationResult('resolved', False), no_response=False)
    for request_id, result in (
        ('old', dict(situation_assessment='unknown', help_needed=True)),
        ('missing', dict(situation_assessment='resolved', help_needed=False)),
        ('new', dict(situation_assessment='unknown', help_needed=True)),
        ('new', dict(situation_assessment='resolved', help_needed=0)),
    ):
        with pytest.raises(ValueError, match='missing or mismatched'):
            receipts.resolve(request_id, result)
    assert receipts.resolve('new', dict(situation_assessment='resolved', help_needed=False)) == 'okay'
