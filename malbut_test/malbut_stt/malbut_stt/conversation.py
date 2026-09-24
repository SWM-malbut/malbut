"""Own dialogue and playback transitions independently of audio and ROS."""

from uuid import uuid4


class ConversationSession:
    """Receive serialized events; callers provide speech and addressee decisions."""

    def __init__(self, *, clock, publish_transcript, publish_control):
        self.clock = clock
        self.publish_transcript = publish_transcript
        self.publish_control = publish_control
        self._seen_playbacks = set()
        self.playback_id = None
        self.playback_state = None
        self._control = None
        self.terminate()

    def activate(self):
        """Enter dialogue without restarting an already active turn."""
        self.active = True

    def activate_proactive(self, session_id):
        """Open an Agent-owned turn; its deadlines are managed by the Agent."""
        self.terminate()
        self.session_id = session_id
        self.active = True
        if self.playback_state in ('playing', 'paused') and self._control != 'stop':
            self._control = 'stop'
            self.publish_control(self.playback_id, 'stop')

    @property
    def interrupted_playback_id(self):
        """Expose the playback associated with the current utterance, if any."""
        return self._interrupted_playback

    def terminate(self):
        """Invalidate pending utterances while preserving actual playback tracking."""
        self.active = False
        self.session_id = ''
        self.deadline = None
        self.utterance_id = None
        self._interrupted_playback = None
        self._resume_pending = False

    def tick(self):
        """Return whether the normal playback completion wait expired."""
        if self.active and self.deadline is not None and self.clock() >= self.deadline:
            self.terminate()
            return True
        return False

    def user_speech_started(self):
        """Reserve one ID and pause the current answer at most once."""
        self.tick()
        if not self.active:
            return None
        if self.utterance_id is not None:
            return self.utterance_id
        self.utterance_id = str(uuid4())
        self.deadline = None
        self._resume_pending = False
        self._interrupted_playback = None
        if self.playback_state in ('playing', 'paused') and self._control != 'stop':
            if self.session_id:
                self._control = 'stop'
                self.publish_control(self.playback_id, 'stop')
                return self.utterance_id
            if self.playback_state == 'playing' or self._control in ('pause', 'resume'):
                self._interrupted_playback = self.playback_id
                if self._control != 'pause':
                    self._control = 'pause'
                    self.publish_control(self.playback_id, 'pause')
        return self.utterance_id

    def discard_utterance(self, utterance_id):
        """Drop an unrecognized utterance without guessing its addressee or playback action."""
        if not self.active or utterance_id != self.utterance_id or utterance_id is None:
            return False
        self.utterance_id = None
        self._interrupted_playback = None
        self._resume_pending = False
        return True

    def finish_utterance(self, utterance_id, text, *, addressed=None):
        """Publish once, or resume an interrupted answer after pause acknowledgement."""
        if not self.active or utterance_id != self.utterance_id or utterance_id is None:
            return False
        if type(addressed) is not bool:
            raise ValueError('an explicit addressee decision is required')
        if addressed and (not isinstance(text, str) or not text.strip()):
            raise ValueError('a final transcript must contain text')
        interrupted = (
            self._interrupted_playback is not None
            and self._interrupted_playback == self.playback_id
            and self.playback_state in ('playing', 'paused')
            and self._control != 'stop'
        )
        self.utterance_id = None
        self._interrupted_playback = None
        if interrupted:
            if addressed:
                self._control = 'stop'
                self.publish_control(self.playback_id, 'stop')
            else:
                self._resume_pending = True
                self._resume_if_paused()
        if addressed:
            self.publish_transcript(utterance_id, text)
        return addressed

    def _resume_if_paused(self):
        if self.active and self._resume_pending and self.playback_state == 'paused':
            self._resume_pending = False
            self._control = 'resume'
            self.publish_control(self.playback_id, 'resume')

    def on_playback_status(self, playback_id, state):
        """Only a new playing event can introduce a playback; terminal states are final."""
        if not isinstance(playback_id, str) or not playback_id.strip():
            raise ValueError('playback ID must contain text')
        if not isinstance(state, str) or state not in (
            'playing', 'paused', 'finished', 'failed', 'stopped',
        ):
            raise ValueError('unknown playback state')
        self.tick()
        if playback_id != self.playback_id:
            if state != 'playing' or playback_id in self._seen_playbacks:
                return
            self._seen_playbacks.add(playback_id)
            self.playback_id = playback_id
            self.playback_state = None
            self._control = None
            self._interrupted_playback = None
            self._resume_pending = False
        if self.playback_state in ('finished', 'failed', 'stopped'):
            return
        previous = self.playback_state
        if state in ('playing', 'paused'):
            if self._control == 'stop':
                return
            self.playback_state = state
            self.deadline = None
            if state == 'playing' and previous == 'paused' and self._control == 'resume':
                self._control = None
            if self.active and state == 'playing' and self.utterance_id is not None:
                if self.session_id:
                    self._control = 'stop'
                    self.publish_control(playback_id, 'stop')
                    return
                self._interrupted_playback = playback_id
                if self._control != 'pause':
                    self._control = 'pause'
                    self.publish_control(playback_id, 'pause')
            self._resume_if_paused()
            return
        self.playback_state = state
        self._resume_pending = False
        self._interrupted_playback = None
        self.deadline = (
            self.clock() + 5.0
            if self.active and not self.session_id and state == 'finished'
            and self._control != 'stop' and self.utterance_id is None
            else None
        )
        self._control = None
