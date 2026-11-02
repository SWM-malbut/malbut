"""Wake -> existing follower -> named room -> camera-confirmed stationary dialogue."""

from collections import deque
import json
from uuid import uuid4

from malbut_agent_server.called_conversation import cancel_approach, prepare_caller_navigation


class CalledConversation:
    """Serialized by the Agent executor; reuse its sensors, speech and motion owner."""

    def __init__(self, chat):
        self.chat, self.node, self.clock = chat, chat.node, chat.clock
        self.enabled = self.node.get_parameter('called_conversation_enabled').value
        self.phase = 'idle'
        self.seen = deque(maxlen=128)
        self.retired = deque(maxlen=32)
        self.session_id = ''
        self.playback_id = ''
        self.future = None
        self.map_check = None
        self.proposal = None
        self.pending = None
        self.input_started = False
        self.moved = False
        self.motion_started = False

    @property
    def active(self):
        return self.phase != 'idle'

    def _available(self):
        chat, now = self.chat, self.clock()
        scene = getattr(self.node, '_scene', None)
        return (not chat._closed and not self.node._closing and not self.node._in_fall_window
                and not (self.node.situation and self.node.situation.active)
                and self.node.count_publishers('/malbut/state') > 0
                and chat._position_system_ok and chat._odom_at is not None
                and 0 <= now - chat._odom_at <= 1.5
                and self.node.dialogue.ready and self.node._speech_ready
                and not self.node.dialogue.has_pending
                and (scene is None or scene._images.permitted()))

    def input_status(self, message):
        if message.state == 'wake' and not message.session_id:
            uid = message.utterance_id
            if not uid or len(uid) > 256 or uid in self.seen:
                return
            self.seen.append(uid)
            if (not self.enabled or self.active or not self._available()
                    or not self.chat._safe() or self.chat.position.busy
                    or self.chat._playing
                    or not self.chat._input.service_is_ready()
                    or self.node.speech_missions is None):
                return  # Ordinary wake listening is already open in STT.
            self.chat.end()
            self.chat.gate.used = True
            self.session_id = 'patrol-chat-called-' + uuid4().hex
            self.request_id = self.session_id
            self.phase, self.deadline = 'adopting', self.clock() + 3
            self.expires = self.clock() + 180
            self.input_started = self.moved = self.motion_started = False
            self.pending = self.proposal = None
            self.questions = 0
            self.future = self.chat._input.call_async(self.chat._session_type.Request(
                session_id=self.session_id, active=True, preserve_input=True))
        elif self.active and (not message.session_id or message.session_id == self.session_id):
            if message.utterance_id in self.seen:
                return  # A transcript can arrive before its separate STARTED topic.
            if message.state == 'started':
                self.input_started = True
                self.deadline = min(self.expires, self.clock() + 60)
                if self.phase in ('acquiring', 'navigating', 'settling'):
                    self._stop('input')
            elif message.state == 'failed':
                self._stop('end')

    def receive(self, message):
        if message.session_id in self.retired:
            return True
        if self.phase == 'stopping' and self.after == 'end' and not self.session_id:
            return False
        if not self.active or message.session_id not in ('', self.session_id):
            return False
        uid, text = message.utterance_id, message.text
        if not uid or len(uid) > 256 or not text or len(text) > 16000:
            self._stop('end')
            return True
        if uid in self.seen:
            return True
        self.seen.append(uid)
        self.input_started = False
        if cancel_approach(text):
            self._stop('cancelled')
        elif self.phase in ('asking', 'locating'):
            self._stop_playback()
            self.phase, self.deadline = 'resolving', self.clock() + 5
            self.future = self.chat._pool.submit(
                prepare_caller_navigation, self.node.speech_missions, uid, text)
        else:
            # Preserve speech received during acquisition/motion, then hand it
            # to ordinary dialogue exactly once after confirmed standstill.
            self.pending = (uid, text)
            self._stop('reply')
        return True

    def speech_request(self, message):
        if not self.active:
            return
        if message.request_id != self.request_id:
            self._stop('end')

    def playback(self, message):
        if (not self.active or message.playback_id != self.playback_id
                or message.request_id != self.request_id):
            return
        if message.state == 'finished' or message.state == 'stopped' and self.input_started:
            self.playback_id = ''
            if self.phase == 'asking':
                self.phase, self.deadline = 'locating', self.clock() + 30
            elif self.phase == 'announcing':
                self.phase, self.deadline = 'dispatching', self.clock() + 5
        elif message.state in ('failed', 'stopped'):
            self._stop('end')

    def _speak(self, text, phase):
        self.phase, self.deadline = phase, self.clock() + 30
        self.playback_id = uuid4().hex
        if not self.node.say(text, request_id=self.request_id, playback_id=self.playback_id):
            self._stop('end')

    def _ask(self):
        if self.moved:
            self._finish('도착했지만 어느 분인지 확인하지 못했어요. 카메라 앞에서 다시 불러 주세요.')
        elif self.questions >= 2:
            self._finish('위치를 확인하지 못해서 이동하지 않았어요. 준비되시면 다시 불러 주세요.')
        else:
            self.questions += 1
            self._speak('어디 계세요? 지도에 등록된 방 이름으로 알려 주세요.', 'asking')

    def _acquire(self):
        if self.chat.position.start():
            self.motion_started = True
            self.phase, self.deadline = 'acquiring', self.clock() + 55
        else:
            self._finish('지금은 가까이 갈 수 없어요. 여기서 말씀을 들을게요.')

    def _stop_playback(self):
        if self.playback_id and self.chat._stop.service_is_ready():
            self.chat._stop.call_async(self.chat._stop_type.Request(
                playback_id=self.playback_id, command=self.chat._stop_type.Request.STOP))
        self.playback_id = ''

    def _stop(self, after):
        self.chat.position.cancel()
        self._stop_playback()
        self.after = after
        self.stop_terminal_seen = False
        self.phase, self.deadline = 'stopping', self.clock() + 30
        if after != 'input':
            self._close_input()

    def _stationary(self):
        now, chat = self.clock(), self.chat
        return (not chat.position.busy and chat._system_idle
                and self.node.count_publishers('/malbut/state') > 0
                and chat._odom_at is not None and 0 <= now - chat._odom_at <= 1.5
                and chat._stationary_since is not None and now - chat._stationary_since >= 2
                and (not self.motion_started or chat.position.released))

    def _settle(self, after):
        self.chat._stationary_since = None
        self.after = after
        self.phase, self.deadline = 'settling', self.clock() + 30

    def tick(self):
        if not self.active:
            return
        now, position = self.clock(), self.chat.position
        allowed = self._available() and now < self.expires and now < self.deadline
        position.tick(allowed and self.phase in ('acquiring', 'navigating'))
        if not allowed and self.phase != 'stopping':
            self._stop('end')
        if self.phase == 'stopping':
            if now >= min(self.deadline, self.expires):
                self._close_input()
                self.pending = None
                self.after = 'end'
            if not position.busy and self.motion_started and not self.stop_terminal_seen:
                self.chat._stationary_since = None
                self.stop_terminal_seen = True
            if self._stationary():
                if self.after == 'input' and self.pending is None and now < self.deadline:
                    return
                pending = self.pending
                after = self.after
                self.close()
                if pending and after == 'reply':
                    from malbut_interfaces.msg import SpeechTranscript
                    self.node._receive_speech(SpeechTranscript(
                        utterance_id=pending[0], text=pending[1]))
                elif after == 'cancelled':
                    self.node.say('알겠어요. 여기서 멈출게요.', request_id=self.request_id)
            # Lost cancellation/result/odometry is not permission to restart.
            return
        if self.input_started:
            return
        if self.phase == 'finishing' and self.future.done():
            try:
                closed = self.future.result().accepted
            except Exception:
                closed = False
            text = self.final_text
            self.close()
            if closed:
                self.node.say(text, request_id='conversation-opener:' + uuid4().hex)
        elif self.phase == 'adopting' and self.future.done():
            try:
                accepted = self.future.result().accepted
            except Exception:
                accepted = False
            self.future = None
            if accepted:
                self._acquire()
            else:
                self.close()  # Original ordinary input/another session remains intact.
        elif self.phase in ('acquiring', 'navigating') and position.phase == 'done':
            if self.phase == 'acquiring':
                self._settle('greet' if position.success else 'ask')
            else:
                self._settle('acquire' if position.success else 'blocked')
        elif self.phase == 'settling' and self._stationary():
            if self.after == 'acquire':
                self._acquire()
            elif self.after == 'ask':
                self._ask()
            elif self.after == 'blocked':
                self._finish('그 구역까지 이동하지 못했어요. 여기서 말씀을 들을게요.')
            else:
                # The follower confirmed one person; require fresh presence
                # again after releasing tracking before opening conversation.
                gate = self.chat.gate
                if (gate.present_since is not None and gate.last_present is not None
                        and 0 <= now - gate.last_present <= 1.5):
                    self.close()
                    self.chat.begin_called_dialogue()
                else:
                    self._ask()
        elif self.phase == 'resolving' and self.future.done():
            try:
                self.proposal = self.future.result()
            except Exception:
                self.proposal = None
            self.future = None
            if self.proposal is None:
                self._ask()
            else:
                self._speak(self.proposal.location + ' 쪽으로 갈게요.', 'announcing')
        elif self.phase == 'dispatching' and not self.chat._playing and self._stationary():
            if self.node.speech_missions.navigation_matches(self.proposal):
                if position.start(json.loads(self.proposal.arguments_json)):
                    self.motion_started = self.moved = True
                    self.phase, self.deadline = 'navigating', now + 120
                    self.map_check_at = now
                else:
                    self._finish('지금은 이동할 수 없어요. 여기서 말씀을 들을게요.')
            else:
                self._finish('지도나 목적지가 변경되어 이동하지 않았어요.')
        if self.phase == 'navigating':
            if self.map_check is not None and self.map_check.done():
                try:
                    valid = self.map_check.result()
                except Exception:
                    valid = False
                self.map_check = None
                if not valid:
                    self._stop('end')
            if self.map_check is None and now >= self.map_check_at:
                self.map_check = self.chat._pool.submit(
                    self.node.speech_missions.navigation_matches, self.proposal)
                self.map_check_at = now + 2

    def _finish(self, text):
        closed = self._close_input()
        self.close()
        if closed is None:
            self.node.say(text, request_id='conversation-opener:' + uuid4().hex)
        else:
            # The opener protocol declines while another microphone session
            # is active. A stop request alone is not proof that it has closed.
            self.future, self.final_text = closed, text
            self.phase, self.deadline = 'finishing', self.clock() + 3

    def _close_input(self):
        future = None
        if self.session_id:
            self.retired.append(self.session_id)
            future = self.chat._input.call_async(self.chat._session_type.Request(
                session_id=self.session_id, active=False))
        self.session_id = ''
        return future

    def close(self):
        self.chat.position.cancel()
        self._stop_playback()
        self._close_input()
        self.phase = 'idle'
        self.pending = None
        for future in (self.future, self.map_check):
            if future is not None:
                future.cancel()
        self.future = self.map_check = None
