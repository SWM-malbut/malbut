"""Bounded, volatile small talk for a camera-visible but unidentified speaker."""

from threading import Lock
import re
import time

from malbut_agent_server.conversation import ConversationSession, ConversationSnapshot, ConversationTurn
from malbut_agent_server.schemas import RobotState, SpeechAgentRequest


class CompanionDialogue:
    """Reuse the provider, never the owner's history, memory, images or tools."""

    def __init__(self, service=None):
        self.service = service
        self._lock = Lock()
        self._session = None
        self._turns = []
        self._ordinal = 0
        self._binding = None
        self._reply_binding = None
        self._reply_expiry = float('inf')
        self._history_expiry = float('inf')
        if self.service:
            self.service.on_change = self.clear_history

    def clear_history(self):
        with self._lock:
            self._turns = []
            self._binding = None
            self._history_expiry = float('inf')

    def snapshot(self):
        """Interruption classification must not borrow the owner's ordinary history."""
        with self._lock:
            if self._session is None:
                return None
            now = self.service.wall() if self.service else time.time()
            allowed = (self._binding is None or self.service.valid(self._binding)) and now < self._history_expiry
            session = ConversationSession(self._session, 'unverified-companion', self._session,
                                          'active', 1, 0, now, now, now + 300)
            return ConversationSnapshot(session, tuple(self._turns) if allowed else (), None)

    def begin(self, session):
        with self._lock:
            if session != self._session:
                self._session, self._turns = session, []
                self._ordinal = 0
                self._binding = None
                self._history_expiry = float('inf')
        if self.service:
            self.service.begin(session)

    def end(self, session=None):
        with self._lock:
            if session is None or session == self._session:
                self._session, self._turns = None, []
                self._binding = None
        if self.service:
            self.service.end(session)

    def reply_receipt(self):
        with self._lock:
            return self._session, self._reply_binding, self._reply_expiry

    def reply_allowed(self, session, receipt=None):
        with self._lock:
            reply_session, binding, expiry = receipt or (self._session, self._reply_binding, self._reply_expiry)
            return (self._session == session == reply_session and (binding is None
                    or self.service.valid(binding) and self.service.wall() < expiry))

    def answer(self, provider, session, identity, text, opener):
        observed_at = self.service.wall() if self.service else time.time()
        if self.service:
            try:
                self.service.sync()
            except Exception:
                pass  # Service invalidates the lease on transport/validation failure.
        binding = self.service.binding(session) if self.service else None
        with self._lock:
            if session != self._session:
                return None
            if binding != self._binding or observed_at >= self._history_expiry:
                self._turns = []
                self._binding = binding
                self._history_expiry = float('inf')
            turns = list(self._turns)
            ordinal = self._ordinal + 1
        normalized = re.sub(r'[\s.!?~。！？]', '', text)
        deletion = normalized in {'일상기억모두지워줘', '일상기억모두삭제해줘',
                                   '방금기억지워줘', '방금기억삭제해줘'}
        if deletion:
            count = self.service.forget(binding, latest=normalized.startswith('방금')) if binding else None
            with self._lock:
                self._turns = []
                self._reply_binding = None
            return ('앱에서 현재 대화 상대를 확인한 뒤 삭제할 수 있어요.' if count is None
                    else '요청하신 일상 기억을 삭제했어요.' if count else '삭제할 일상 기억이 없어요.')
        memories = self.service.context(binding, query=text) if binding else []
        scene = {'companion_guest': binding is None}
        if binding:
            scene.update(companion_verified=True, daily_memories=memories)
        if opener:
            scene['conversation_opener'] = opener
        request = SpeechAgentRequest(
            request_id=identity, turn_id=identity, conversation_id=session,
            user_id='unverified-companion' if binding is None else 'app-verified-companion', utterance=text,
            robot_state=RobotState(camera_available=False),
            scene_context=scene, available_tools=(),
        )
        if binding and not self.service.valid(binding):
            return None
        result = provider.complete(request, [], turns, [])
        decision = result.decision
        if (decision.type not in ('message', 'clarification', 'refusal')
                or not isinstance(decision.message, str) or not decision.message.strip()):
            message = '이 대화에서는 기기를 조작하거나 개인 기억을 바꾸지 않아요. 필요하면 제이크라고 불러 주세요.'
        else:
            message = decision.message[:2000]
        now = time.time()
        if binding:
            try:
                self.service.remember(binding, identity, text, observed_at)
            except Exception:
                # Failed extraction must not persist raw text as a retry job.
                pass
            if not self.service.valid(binding):
                return None
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
            self._reply_binding = binding
            self._history_expiry = min(self._history_expiry,
                                       min((m['expires_at'] for m in memories), default=float('inf')))
            self._reply_expiry = self._history_expiry
            self._turns.append(turn)
            self._ordinal = ordinal
            # Bound retained context independently of the transcript size limit.
            while (len(self._turns) > 8 or sum(len(t.user_content) + len(t.assistant_content)
                                             for t in self._turns) > 8000):
                self._turns.pop(0)
        return message
