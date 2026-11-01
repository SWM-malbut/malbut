"""Bounded, volatile small talk for a camera-visible but unidentified speaker."""

from threading import Lock
import time

from malbut_agent_server.conversation import ConversationTurn
from malbut_agent_server.schemas import RobotState, SpeechAgentRequest


class CompanionDialogue:
    """Reuse the provider, never the owner's history, memory, images or tools."""

    def __init__(self):
        self._lock = Lock()
        self._session = None
        self._turns = []
        self._ordinal = 0

    def begin(self, session):
        with self._lock:
            if session != self._session:
                self._session, self._turns = session, []
                self._ordinal = 0

    def end(self, session=None):
        with self._lock:
            if session is None or session == self._session:
                self._session, self._turns = None, []

    def answer(self, provider, session, identity, text, opener):
        with self._lock:
            if session != self._session:
                return None
            turns = list(self._turns)
            ordinal = self._ordinal + 1
        scene = {'companion_guest': True}
        if opener:
            scene['conversation_opener'] = opener
        request = SpeechAgentRequest(
            request_id=identity, turn_id=identity, conversation_id=session,
            user_id='unverified-companion', utterance=text,
            robot_state=RobotState(camera_available=False),
            scene_context=scene, available_tools=(),
        )
        result = provider.complete(request, [], turns, [])
        decision = result.decision
        if (decision.type not in ('message', 'clarification', 'refusal')
                or not isinstance(decision.message, str) or not decision.message.strip()):
            message = '이 대화에서는 기기를 조작하거나 개인 기억을 바꾸지 않아요. 필요하면 제이크라고 불러 주세요.'
        else:
            message = decision.message[:2000]
        now = time.time()
        turn = ConversationTurn(
            conversation_id=session, user_id='unverified-companion',
            session_instance_id=session, turn_id=identity, request_id=identity,
            request_fingerprint='', generation=1, ordinal=ordinal,
            user_content=text, assistant_content=message, response={},
            created_at=now, completed_at=now,
        )
        with self._lock:
            if session != self._session:
                return None
            self._turns.append(turn)
            self._ordinal = ordinal
            # Bound retained context independently of the transcript size limit.
            while (len(self._turns) > 8 or sum(len(t.user_content) + len(t.assistant_content)
                                             for t in self._turns) > 8000):
                self._turns.pop(0)
        return message
