"""Keep answer provenance inside the Agent process, outside the ROS contract."""

from collections import OrderedDict
from threading import Lock


class ConfirmationAnswers:
    """Share completed answers between dialogue and fall-rule executor threads.

    Manager still receives the existing two-field Action result. A matching
    successful result may use this receipt to distinguish silence from help.
    Transport errors never consult these receipts.
    """

    def __init__(self, limit=128):
        self._limit = limit
        self._completed = OrderedDict()
        self._lock = Lock()

    def remember(self, request_id, outcome, *, no_response):
        if (not isinstance(request_id, str) or not request_id
                or type(no_response) is not bool
                or (no_response and not outcome.help_needed)):
            raise ValueError('invalid confirmation answer receipt')
        answer = 'no_response' if no_response else 'help' if outcome.help_needed else 'okay'
        value = (outcome.situation_assessment, outcome.help_needed, answer)
        with self._lock:
            if request_id in self._completed and self._completed[request_id] != value:
                raise ValueError('conflicting confirmation answer receipt')
            self._completed[request_id] = value
            self._completed.move_to_end(request_id)
            while len(self._completed) > self._limit:
                self._completed.popitem(last=False)

    def resolve(self, request_id, result):
        with self._lock:
            receipt = self._completed.get(request_id)
        if (receipt is None or type(result.get('help_needed')) is not bool
                or receipt[:2] != (result.get('situation_assessment'), result['help_needed'])):
            # An unknown/mismatched receipt is not evidence of silence or help.
            raise ValueError('missing or mismatched confirmation answer receipt')
        return receipt[2]
