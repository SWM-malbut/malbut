"""Own dialogue and playback transitions independently of audio and ROS."""

import re
import unicodedata
from uuid import uuid4

from malbut_stt.wake import parse_wake_phrase


_TALK = r'(?:연속대화|대화모드|대화|이야기|얘기)'
_PARTICLE = r'[은는을를]?'
_DO = (r'(?:하자|하죠|하세|합시다|하시죠|하세요|할까(?:요)?|할래(?:요)?|할게(?:요)?|'
       r'하겠습니다|하고싶(?:어(?:요)?|습니다)|해(?:요|줘(?:요)?|주세요|줄래(?:요)?|도돼(?:요)?)?)')
_REQUEST = r'(?:요|줘(?:요)?|주세요|줄래(?:요)?)?'
_FINISH = (rf'(?:끝내{_REQUEST}|끝내자|끝낼(?:게|래|까)(?:요)?|끝냅시다|'
           rf'마쳐{_REQUEST}|마치자|마치죠|마칩시다|마칠(?:게|래|까)(?:요)?|마치겠습니다|'
           rf'끝내고싶(?:어(?:요)?|습니다)|마무리{_DO}|(?:종료|중단|중지)(?:{_DO})?|멈춰{_REQUEST}|멈추자)')
_DISLIKE = r'싫(?:어(?:요)?|습니다)'
_DONT_WANT = r'싶지않(?:아(?:요)?|습니다)'
_LATER = r'(?:나중에|다음에|이따(?:가)?|내일|시간될때|필요하면|필요할때)(?:다시|또|마저)?'
_NEXT_TALK = rf'{_LATER}(?:{_TALK}|말)(?:나누자|나눠요|{_DO})'
_NO_MORE = r'(?:없어(?:요)?|없습니다|없네(?:요)?)'
_ENOUGH = r'(?:됐어(?:요)?|됐습니다|충분해(?:요)?|충분합니다)'
_DONT = rf'(?:마(?:세요|요)?|말(?:자|아요)|말아{_REQUEST})'
# Whole clauses only. A bare "그만" or "멈춰" can also cancel a robot mission.
_END_FORMS = (
    # Finish the conversation or close continuous listening.
    rf'{_TALK}{_PARTICLE}(?:좀|이제|이만|여기서)?(?:그만(?:{_DO})?|{_FINISH}|끝)',
    rf'(?:연속대화|대화모드){_PARTICLE}(?:꺼{_REQUEST}|끄자)',
    rf'대화창{_PARTICLE}닫아{_REQUEST}',
    rf'그만(?:좀)?(?:{_TALK}(?:{_DO})?|(?:말|대답|질문){_DO})',
    rf'(?:말|질문){_PARTICLE}(?:좀)?(?:그만(?:{_DO})?|멈춰{_REQUEST})',
    r'그만(?:하자|합시다|할(?:게|래|까)(?:요)?|하고싶어(?:요)?)',
    r'끝내자|마치자|마칩시다|마무리하자',
    # Stop here, or indicate that the conversation has run its course.
    rf'(?:{_TALK}{_PARTICLE})?여기까지만?(?:야|예요|입니다|{_DO}|{_FINISH})?',
    rf'(?:{_TALK}{_PARTICLE})?(?:여기서|이쯤(?:에서)?)(?:{_DO}|{_FINISH})',
    rf'(?:이제|이만|오늘은)그만|(?:오늘은|이제)이만{_DO}|이만{_FINISH}',
    rf'(?:질문|답변){_PARTICLE}여기까지만?(?:{_DO})?',
    rf'(?:그만하면|이만하면|이정도면|그정도면){_ENOUGH}',
    r'(?:오늘은|그럼|그러면)이만|(?:이만|여기서)끊(?:자|을게(?:요)?|겠습니다)',
    # Decline further speech, questions or replies.
    rf'(?:{_TALK}|말){_PARTICLE}(?:하기{_DISLIKE}|하고{_DONT_WANT}|안할래(?:요)?|하지{_DONT})',
    rf'아무말도하고{_DONT_WANT}|듣(?:기{_DISLIKE}|고{_DONT_WANT})|그만듣고싶어(?:요)?',
    r'그만들을래요?|더안들어도돼요?',
    r'충분히들었어(?:요)?',
    rf'(?:할말|물어볼(?:게|거|것)|궁금한(?:게|건|거|것))(?:이|은|는)?{_NO_MORE}',
    r'궁금한(?:건|거|것은)다물어봤어(?:요)?',
    rf'(?:(?:아무말도|말|대꾸|대답|답변|질문)하|묻|물어보|말시키)지{_DONT}',
    r'(?:말|답|대답|답변)(?:은|는)?(?:안해도돼(?:요)?|하지않아도돼(?:요)?|됐어(?:요)?)',
    rf'(?:답|답변|대답)(?:은|이)?필요{_NO_MORE}',
    rf'조용히(?:좀)?(?:해|있어){_REQUEST}|(?:입좀?다물어|닥쳐){_REQUEST}',
    rf'혼자있고싶(?:어(?:요)?|습니다)|혼자있게해{_REQUEST}|(?:지금은|잠깐)말걸지{_DONT}',
    # Defer talking or explicitly say the next contact will be later.
    _NEXT_TALK, rf'(?:{_TALK}{_PARTICLE}|얘긴){_LATER}{_DO}', rf'(?:여기까지만?|그만)하고{_NEXT_TALK}',
    r'(?:나중에|다음에|이따가)(?:다시)?(?:하자|합시다|해요)',
    rf'{_LATER}(?:보자|봐(?:요)?|뵐게요|부를게(?:요)?|올게(?:요)?|말걸게(?:요)?)',
    rf'(?:다시)?부를때까지기다려{_REQUEST}|호출어대기(?:모드)?로돌아가{_REQUEST}',
    # Leave, rest or say goodbye. A bare greeting or thank-you is not a farewell.
    r'(?:가볼게|갈게|들어갈게|들어가볼게|자러갈게|쉴게|잘게)(?:요)?|가봐야겠(?:어(?:요)?|다)|자야겠다',
    r'쉬고싶(?:어(?:요)?|습니다)|쉬러갈게(?:요)?',
    r'잘(?:가|자|있어)(?:요)?|안녕히(?:가|계|주무)(?:세요|십시오)|좋은밤(?:되세요|보내세요)',
    r'바이바이|굿나잇',
)
_COURTESY = (r'(?:응|네|그래요?|아니요?|알겠(?:어(?:요)?|습니다)|알았어요?|고마워요?|고마웠어요?|'
             r'고맙습니다|감사합니다|감사해요?|(?:수고|고생)했어요?|수고하셨습니다|잘들었어요?|'
             rf'괜찮아요?|{_ENOUGH}|제이크(?:야)?)')
_LEAD = (r'(?:어|그럼|그러면|그냥|이제는?|이만|오늘은?|지금은?|우리|나는?|난|저는?|먼저|좀|잠깐|'
         r'더이상|더는?|피곤해서|바빠서)')
_END_CLAUSE = re.compile(
    rf'(?:{_COURTESY}|{_LEAD}){{0,5}}(?:' + '|'.join(_END_FORMS) + rf'){_COURTESY}{{0,2}}')
_ASIDE = re.compile(rf'(?:{_COURTESY}|{_LEAD}){{1,5}}')
_STATE_QUESTION = re.compile(r'(?:없어|싶어|싫어|싶지않아|들었어|충분해)(?:요)?$')


def is_conversation_end(text):
    """Require a closing intent and leave quotes, new requests and negations intact."""
    command = parse_wake_phrase(text) or text
    compact = ''.join(unicodedata.normalize('NFC', command).split())
    found_end = False
    for clause, punctuation in re.findall(r'([^,，.!?…。！？~]+)([,，.!?…。！？~]*)', compact):
        if _END_CLAUSE.fullmatch(clause):
            # "더 할 말 없어?" invites the robot to speak. "여기까지 할까요?" closes.
            if ('?' in punctuation or '？' in punctuation) and _STATE_QUESTION.search(clause):
                return False
            found_end = True
        elif not _ASIDE.fullmatch(clause):
            return False
    return found_end


class ConversationSession:
    """Receive serialized events; callers provide speech and addressee decisions."""

    def __init__(self, *, clock, publish_transcript, publish_control):
        self.clock = clock
        self.publish_transcript = publish_transcript
        self.publish_control = publish_control
        self._seen_playbacks = set()
        self.playback_id = None
        self.playback_state = None
        self._playback_interim = False
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

    def user_speech_started(self, *, stop_playback=False):
        """Reserve one ID and pause or stop the current answer at most once."""
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
            if self.session_id or stop_playback:
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

    def on_playback_status(self, playback_id, state, *, interim=False):
        """Only a new playing event can introduce a playback; terminal states are final."""
        if not isinstance(playback_id, str) or not playback_id.strip():
            raise ValueError('playback ID must contain text')
        if not isinstance(state, str) or state not in (
            'playing', 'paused', 'finished', 'failed', 'stopped',
        ):
            raise ValueError('unknown playback state')
        if type(interim) is not bool:
            raise ValueError('interim must be a boolean')
        self.tick()
        if playback_id != self.playback_id:
            if state != 'playing' or playback_id in self._seen_playbacks:
                return
            self._seen_playbacks.add(playback_id)
            self.playback_id = playback_id
            self.playback_state = None
            self._playback_interim = interim
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
            and not self._playback_interim and self._control != 'stop'
            and self.utterance_id is None
            else None
        )
        self._control = None
