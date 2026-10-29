"""Keep continuous capture and local inference outside serialized dialogue events."""

import math
import os
from pathlib import Path
import json
from collections import OrderedDict, deque
from contextlib import nullcontext
from dataclasses import dataclass, replace
from queue import Empty, Full, Queue
from threading import Event, Thread
from time import monotonic

from malbut_stt.audio import CapturePCM, CaptureSettings, MicrophoneOverflow
from malbut_stt.conversation import ConversationSession
from malbut_stt.endpoint import is_complete_korean_utterance
from malbut_stt.pipeline import pcm_bytes
from malbut_stt.streaming import StreamingUtteranceCollector
from malbut_stt.wake import parse_wake_phrase


MAX_RETIRED_SESSION_IDS = 256
MICROPHONE_TIMEOUT_SECONDS = 5.0
RETRY_NOTICE_TIMEOUT_SECONDS = 45.0


class _SpeechPCM(bytes):
    """Keep voiced duration with an immutable non-streaming inference snapshot."""

    def __new__(cls, pcm, voiced_s, endpoint_pcm=None, transcriber=None):
        value = super().__new__(cls, pcm)
        value.voiced_s = voiced_s
        value.endpoint_pcm = endpoint_pcm
        value.transcriber = transcriber
        return value


def _short_speech_rejection(text, voiced_s):
    """Conservative Korean length check, not a transcript blacklist.

    This heuristic only covers <=600 ms of VAD-positive audio. Six syllables
    remain allowed for short replies; longer text needs up to 20 syllables/s.
    VAD can miss quiet speech, so this is a tunable safeguard, not confidence.
    """
    if not isinstance(text, str) or voiced_s is None or voiced_s > 0.6:
        return None
    syllables = sum('\uac00' <= char <= '\ud7a3' for char in text)
    maximum = max(6, math.ceil(voiced_s * 20))
    if syllables > maximum:
        return dict(reason='short_speech_text_mismatch', voiced_s=voiced_s,
                    korean_syllables=syllables, max_korean_syllables=maximum)
    return None


@dataclass(frozen=True)
class _StreamInput:
    pcm: bytes
    stream: object
    final: bool
    speech_end_s: float
    audio_start_s: float = 0.0
    voiced_s: float | None = None
    endpoint_pcm: bytes | None = None


class DialoguePipeline:
    """Run one capture thread and one ASR worker; poll and callbacks share one owner.

    One utterance may await inference or addressee classification. Additional
    utterances are explicitly discarded through their endpoint while it is busy.
    An accepted ordinary request normally blocks capture until its final reply
    terminates. Reachy mode opens capture during the matching final playback,
    using an explicitly configured echo-cancelled microphone input.
    Successful replies open a bounded, wake-free follow-up window; failed replies
    return to wake listening. Agent-owned sessions keep their policy.
    ``input_has_aec`` asserts that the selected microphone already supplies AEC;
    this class does not remove playback echo from raw microphone audio.
    """

    def __init__(self, *, recorder_factory, wake, transcriber, is_speech,
                 publish_transcript, publish_control, publish_interruption,
                 report, settings=None, input_has_aec=False, clock=monotonic,
                 on_wake=None, endpoint_predecode_s: float | None = None,
                 partial_interval_s: float | None = 2.0, on_partial=None,
                 on_endpoint=None, publish_input_status=None, diagnostics=None,
                 stop_speech=None, on_failure=None, chime_tail_s=0.3,
                 short_speech_guard=False, followup_timeout_s=10.0,
                 validate_wake_command=None, start_qualifier=None, smart_turn=None,
                 wake_is_speech=None, barge_in_ducking=False, barge_in_mode='legacy',
                 interruption_transcriber=None, barge_in_pre_roll_s=2.0):
        if type(barge_in_ducking) is not bool:
            raise ValueError('barge_in_ducking must be a boolean')
        if barge_in_mode not in ('legacy', 'reachy'):
            raise ValueError('barge_in_mode must be legacy or reachy')
        if barge_in_mode == 'reachy' and input_has_aec is not True:
            raise ValueError('Reachy barge-in requires an echo-cancelled input')
        self.barge_in_mode = barge_in_mode
        self.barge_in_ducking = barge_in_ducking or barge_in_mode == 'reachy'
        self._barge_reply_playback_id = None
        self._ducked_playback = None
        self._duck_hold_until = 0.0
        self._duck_renew_at = 0.0
        self._duck_cycle = 0
        self._duck_clear_after = None
        self._duck_lease_until = None
        self._seen_openers = deque(maxlen=128)
        if (type(chime_tail_s) not in (int, float) or not math.isfinite(chime_tail_s)
                or not 0 <= chime_tail_s <= 3):
            raise ValueError('chime_tail_s must be finite and between zero and three seconds')
        if type(short_speech_guard) is not bool:
            raise ValueError('short_speech_guard must be a boolean')
        if (type(followup_timeout_s) not in (int, float)
                or not math.isfinite(followup_timeout_s) or followup_timeout_s < 0):
            raise ValueError('followup_timeout_s must be finite and nonnegative')
        self.chime_tail_s = chime_tail_s
        self.short_speech_guard = short_speech_guard
        self.followup_timeout_s = followup_timeout_s
        self.validate_wake_command = validate_wake_command
        self.smart_turn = smart_turn
        self._smart_turn_result = None
        self.recorder_factory = recorder_factory
        self.wake = wake
        self.transcriber = transcriber
        self.interruption_transcriber = interruption_transcriber
        self._capture_transcriber = None
        self.publish_transcript = publish_transcript
        self.publish_interruption = publish_interruption
        self.publish_control = publish_control
        # stop_speech(playback_id) drops Agent speech; an empty ID drops all of it.
        self.stop_speech = stop_speech
        self._report = report
        self.diagnostics = diagnostics
        self._observe_audio = getattr(diagnostics, 'audio', None)
        self._diagnostic_error_reported = False
        self.clock = clock
        self._event_time = None
        self.on_wake = on_wake
        self.on_endpoint = on_endpoint
        self.on_failure = on_failure
        self.on_partial = on_partial
        self.publish_input_status = publish_input_status
        self._stream_factory = getattr(transcriber, 'create_stream', None)
        self._stream = None
        self.input_has_aec = input_has_aec
        self.session = ConversationSession(
            clock=lambda: self.clock() if self._event_time is None else self._event_time,
            publish_transcript=self._publish_transcript,
            publish_control=self._publish_control,
        )
        settings = settings or CaptureSettings(
            silence_timeout_s=2.0,
            max_utterance_s=None if callable(self._stream_factory) else 20.0,
        )
        if (type(barge_in_pre_roll_s) not in (int, float)
                or not math.isfinite(barge_in_pre_roll_s) or not 0 <= barge_in_pre_roll_s <= 3):
            raise ValueError('barge-in pre-roll must be between zero and three seconds')
        if barge_in_mode == 'reachy':
            settings = replace(settings, pre_roll_s=max(settings.pre_roll_s,
                min(barge_in_pre_roll_s, settings.start_timeout_s, settings.max_buffer_s)))
        if endpoint_predecode_s is not None and (
            isinstance(endpoint_predecode_s, bool)
            or not math.isfinite(endpoint_predecode_s)
            or not 0 < endpoint_predecode_s <= 1.0 < settings.silence_timeout_s
        ):
            raise ValueError('predecode must start by 1.0 seconds before the fallback')
        observe_vad = getattr(diagnostics, 'wrap_vad', None)
        wake_is_speech = is_speech if wake_is_speech is None else wake_is_speech
        self.command_stream = StreamingUtteranceCollector(
            observe_vad(is_speech, kind='command') if callable(observe_vad) else is_speech,
            settings=settings,
            early_endpoint_s=(endpoint_predecode_s if endpoint_predecode_s is not None
                              else 1.0 if settings.silence_timeout_s > 1.0 else None),
            partial_interval_s=(partial_interval_s if callable(self._stream_factory) else None),
            start_qualifier=start_qualifier,
        )
        self.wake_stream = StreamingUtteranceCollector(
            observe_vad(wake_is_speech, kind='wake') if callable(observe_vad) else wake_is_speech,
            settings=CaptureSettings(silence_timeout_s=0.4, max_utterance_s=6.0),
        )
        self.audio = Queue(maxsize=64)
        self.jobs = Queue(maxsize=1)
        self.results = Queue(maxsize=1)
        self.stopping = Event()
        self.overflow = Event()
        self.capture_ready = Event()
        self.recorder = None
        self.capture_thread = None
        self.asr_thread = None
        self.capture_error = None
        self._last_capture_at = None
        self.phase = 'idle'
        self._started = False
        self._closed = False
        self._generation = 0
        self._audio_generation = 0
        self._busy = False
        self._capture_id = None
        self._discard_capture = False
        self._utterance_playback_id = None
        self._pending = None
        self._reply_request_id = None
        self._followup_pending = False
        self._followup_deadline = None
        self._retry_notice_deadline = None
        self._command_start_deadline = None
        self._web_talk_lease_id = None
        self._web_talk_deadline = 0.0
        self._web_talk_drain_until = 0.0
        self._web_talk_quiet = False
        self._retired_session_ids = OrderedDict()
        self._raw_playback_gate = False
        self._raw_gate_until = 0.0
        self._raw_tail_s = 0.3
        self._chime_playing = False
        self._chime_gate_until = 0.0
        self._tail_stream = None
        self._endpoint_job = None
        self._endpoint_is_partial = False
        self._endpoint_requested_at = None
        self._endpoint_candidate = None
        self._endpoint_result = None
        self._endpoint_final = None
        self._final_input = None

    @property
    def pending_addressee(self):
        """Expose correlation and deadline without exposing held transcript text."""
        if self._pending is None:
            return None
        return self._pending[0], self._pending[1], self._pending[3]

    def report(self, event):
        self._report(event)
        if self.diagnostics is not None:
            self.diagnostics.event(
                event, clock=self.clock(), generation=self._generation,
                audio_generation=self._audio_generation, session_id=self.session.session_id,
                utterance_id=self.session.utterance_id, capture_id=self._capture_id,
                reply_request_id=self._reply_request_id,
                chime_gate_until=self._chime_gate_until,
                chime_tail_s=self.chime_tail_s,
                command_start_deadline=self._command_start_deadline,
                followup_timeout_s=self.followup_timeout_s,
                followup_deadline=self._followup_deadline)
            if self.diagnostics.error and not self._diagnostic_error_reported:
                self._diagnostic_error_reported = True
                self._report('diagnostics_stopped')

    def start(self):
        """Open one microphone and start the bounded capture and inference workers."""
        self.phase = 'opening_microphone'
        self.recorder = self.recorder_factory()
        if self.recorder.sample_rate != 16000:
            raise ValueError('speech capture requires 16kHz PCM')
        self.phase = 'starting_microphone'
        self.recorder.start()
        self._check_aec_health()
        calibration = getattr(self.recorder, 'calibration', None)
        if self.input_has_aec and isinstance(calibration, dict):
            self.report('aec_ready:' + json.dumps(calibration, sort_keys=True))
        self._started = True
        self._last_capture_at = monotonic()
        self.capture_thread = Thread(target=self._capture, name='stt-capture', daemon=True)
        self.asr_thread = Thread(target=self._infer, name='stt-asr', daemon=True)
        self.capture_thread.start()
        self.asr_thread.start()
        self.phase = 'running'
        self.report('waiting_for_wake')

    @property
    def barge_in_available(self):
        return bool(self.input_has_aec and getattr(self.recorder, 'aec_ready', True)
                    and self.capture_ready.is_set() and self.capture_error is None
                    and not self._capture_timed_out())

    def start_session(self, session_id):
        """Replace ordinary dialogue with a correlated, wake-free confirmation."""
        self._expire_web_talk()
        if (self.stopping.is_set() or self.capture_error is not None
                or self._web_talk_blocked(self.clock())
                or self._capture_timed_out() or not isinstance(session_id, str)
                or not session_id.strip() or len(session_id) > 200):
            return False
        if session_id in self._retired_session_ids:
            return False
        if self.session.active and self.session.session_id == session_id:
            return True
        self._terminate('session_replaced:proactive')
        self._tail_stream = None
        self.session.activate_proactive(session_id)
        return True

    def stop_session(self, session_id):
        """Close this ID or reserve its closure without touching another session."""
        if (self.stopping.is_set() or not isinstance(session_id, str)
                or not session_id.strip() or len(session_id) > 200):
            return False
        if self.session.session_id != session_id:
            if session_id in self._retired_session_ids:
                return False
            self._retire_session(session_id)
            return True
        self._terminate('session_ended:proactive')
        return True

    def session_is_active(self, session_id):
        """Read the current session without creating, closing, or retiring any ID."""
        self._expire_web_talk()
        return bool(
            not self.stopping.is_set()
            and not self._web_talk_blocked(self.clock())
            and self.capture_error is None and not self._capture_timed_out()
            and isinstance(session_id, str) and session_id.strip()
            and len(session_id) <= 200
            and self.session.active and self.session.session_id == session_id)

    def control_web_talk(self, lease_id, active, ttl_s, *, quiet=True):
        """Acknowledge a web microphone lease only after invalidating local speech.

        ``quiet`` also keeps the Agent silent while the guardian talks; the
        startup quarantine only gates input.
        """
        if (self.stopping.is_set() or not isinstance(lease_id, str)
                or not lease_id.strip() or len(lease_id) > 200 or type(active) is not bool):
            return False
        if active and (isinstance(ttl_s, bool) or not isinstance(ttl_s, (int, float))
                       or not math.isfinite(ttl_s) or not 0 < ttl_s <= 15.0):
            return False
        self._expire_web_talk()
        if not active:
            if lease_id != self._web_talk_lease_id:
                return False
            self._end_web_talk('web_talk_ended')
            return True
        was_active = self._web_talk_lease_id is not None
        self._web_talk_lease_id = lease_id
        self._web_talk_deadline = self.clock() + ttl_s
        if not was_active:
            if self.session.session_id:
                self._input_status('failed', '')
            reply_request_id = self._reply_request_id
            retry_notice_deadline = self._retry_notice_deadline
            self._terminate('web_talk_started')
            # A web call cannot complete an already accepted ordinary request.
            self._reply_request_id = reply_request_id
            self._retry_notice_deadline = retry_notice_deadline
            self._tail_stream = None
            self._drain(self.results, lambda result: self._trace_job(
                'result_rejected', *result[:3], reason='web_talk_started'))
        if quiet and not self._web_talk_quiet:
            # The guardian owns the speaker: drop current and queued speech for good.
            self._web_talk_quiet = True
            self._stop_speech('')
        return True

    def on_speech_request(self, playback_id, *, request_id='', interim=False):
        """Cancel Agent speech requested during a web talk before it is played."""
        if self.stopping.is_set():
            return
        self._expire_web_talk()
        if (self._web_talk_quiet and isinstance(playback_id, str)
                and playback_id.strip() and len(playback_id) <= 200):
            self._stop_speech(playback_id)
            return
        if (not interim and isinstance(request_id, str)
                and request_id.startswith('conversation-opener:') and len(request_id) <= 200
                and isinstance(playback_id, str) and playback_id.strip()):
            if request_id in self._seen_openers:
                return
            self._seen_openers.append(request_id)
            if (self.session.active or self._reply_request_id is not None
                    or self._busy or self._pending is not None
                    or self._web_talk_blocked(self.clock())
                    or self.wake_stream.collector.started
                    or self.wake_stream.collector.candidate_audio
                    or (self.session.playback_state in ('playing', 'paused')
                        and self.session.playback_id != playback_id)):
                self._stop_speech(playback_id)
                self.report('conversation_opener_declined:busy')
                return
            self._terminate('conversation_opener')
            self._reply_request_id = request_id
            self._followup_pending = self.followup_timeout_s > 0
            self._retry_notice_deadline = self.clock() + RETRY_NOTICE_TIMEOUT_SECONDS
            # Different ROS topics can deliver PLAYING before the request.
            if self.session.playback_id == playback_id and self.session.playback_state:
                self.on_playback_status(playback_id, self.session.playback_state,
                                        request_id=request_id)

    def _stop_speech(self, playback_id):
        if self.stop_speech is not None:
            self.stop_speech(playback_id)
            self.report('web_talk_speech_stopped')

    def _web_talk_blocked(self, captured_at):
        return (self._web_talk_lease_id is not None
                or captured_at < self._web_talk_drain_until)

    def _expire_web_talk(self):
        # Only the owner thread mutates dialogue; capture stays gated until this runs.
        if self._web_talk_lease_id is not None and self.clock() >= self._web_talk_deadline:
            self._end_web_talk('web_talk_expired')

    def _end_web_talk(self, reason):
        self._web_talk_drain_until = self.clock() + 0.3
        self._reset_audio()
        self._web_talk_lease_id = None
        self._web_talk_quiet = False
        self.report(reason)

    def _capture_timed_out(self):
        # Device liveness is independent of the injected dialogue-policy clock.
        return (self._started and self._last_capture_at is not None
                and monotonic() - self._last_capture_at >= MICROPHONE_TIMEOUT_SECONDS)

    def _retire_session(self, session_id):
        if session_id:
            self._retired_session_ids[session_id] = None
            self._retired_session_ids.move_to_end(session_id)
            while len(self._retired_session_ids) > MAX_RETIRED_SESSION_IDS:
                self._retired_session_ids.popitem(last=False)

    def _input_status(self, state, uid=None):
        uid = (self.session.utterance_id or '') if uid is None else uid
        if state == 'started':
            self._timing('speech_started', uid)
        # Ordinary turns have no session ID, but must identify a real utterance.
        # Proactive sessions also use a blank UID for session-wide input failure.
        if self.publish_input_status is not None and (
                self.session.session_id or isinstance(uid, str) and uid.strip()):
            self.publish_input_status(
                self.session.session_id, uid, state)

    def _publish_transcript(self, utterance_id, text):
        self._timing('transcript_final', utterance_id)
        if not self.session.session_id:
            # Keep capture closed until this request's final reply actually ends.
            self._terminate('waiting_for_reply')
            self._reply_request_id = utterance_id
            self._followup_pending = self.followup_timeout_s > 0
            self._tail_stream = None
        self.publish_transcript(utterance_id, text)

    def _timing(self, stage, uid, **fields):
        if not uid:
            return
        try:
            self._report(json.dumps(dict(event='speech_timing', version=1,
                stage=stage, request_id=uid, at_s=self.clock(), **fields)))
        except Exception:
            pass  # Observability must not interrupt capture.

    def _capture(self):
        try:
            while not self.stopping.is_set():
                generation = self._audio_generation
                blocked = self._input_blocked(self.clock())
                busy = self._busy or self._pending is not None
                try:
                    samples = self.recorder.read()
                except MicrophoneOverflow:
                    # A PortAudio discontinuity invalidates the utterance just
                    # like our queue overflowing; it does not kill the device.
                    self.overflow.set()
                    continue
                captured_at = self.clock()
                if self.stopping.is_set():
                    break
                if self._capture_timed_out():
                    raise RuntimeError('microphone input timeout')
                # Quiet frames and echo-gated frames still prove capture is alive.
                self._last_capture_at = monotonic()
                self.capture_ready.set()
                if callable(self._observe_audio):
                    self._observe_audio(
                        pcm_bytes(samples), captured_at=captured_at,
                        audio_generation=generation,
                        blocked=blocked or self._input_blocked(captured_at),
                        busy=busy, overflow=self.overflow.is_set())
                if (self.overflow.is_set() or generation != self._audio_generation
                        or blocked or self._input_blocked(captured_at)):
                    continue
                try:
                    self.audio.put_nowait((
                        generation, captured_at, pcm_bytes(samples),
                        busy or self._busy or self._pending is not None,
                    ))
                except Full:
                    self.overflow.set()
        except Exception as error:
            if not self.stopping.is_set() and self.capture_error is None:
                self.capture_error = error

    def _trace_job(self, event, kind, generation, uid, **fields):
        if self.diagnostics is not None:
            self.diagnostics.event(
                event, kind=kind, generation=generation,
                utterance_id=uid[0] if isinstance(uid, tuple) else uid,
                revision=uid[1] if isinstance(uid, tuple) else None, **fields)

    def _enqueue_job(self, job):
        try:
            self.jobs.put_nowait(job)
        except Full:
            self._trace_job('job_rejected', *job[:3], reason='jobs_queue_full')
            raise
        self._trace_job('job_enqueued', *job[:3])

    def _infer(self):
        while not self.stopping.is_set():
            try:
                kind, generation, uid, pcm = self.jobs.get(timeout=0.05)
            except Empty:
                continue
            self._trace_job('job_dequeued', kind, generation, uid)
            if self.stopping.is_set():
                self._trace_job('job_discarded', kind, generation, uid, reason='shutdown')
                break
            text, error_name = None, None
            turn_complete = None
            retained_start_s = 0.0
            context = (self.diagnostics.context(
                kind=kind, generation=generation, session_id=self.session.session_id,
                utterance_id=uid[0] if isinstance(uid, tuple) else uid,
                revision=uid[1] if isinstance(uid, tuple) else None,
                audio_start_s=getattr(pcm, 'audio_start_s', 0.0),
                speech_end_s=getattr(pcm, 'speech_end_s', None),
                final=getattr(pcm, 'final', kind == 'command'),
                voiced_s=getattr(pcm, 'voiced_s', None),
            ) if self.diagnostics is not None else nullcontext())
            with context:
                error_detail = None
                try:
                    # A reset can cancel a preview before the worker picks it up.
                    # Still return through results so endpoint ownership is released.
                    if generation != self._generation:
                        raise RuntimeError('stale inference job')
                    if kind == 'endpoint' and self.smart_turn is not None:
                        try:
                            prediction = self.smart_turn.check(pcm.endpoint_pcm)
                            turn_complete = prediction.complete
                            if self.diagnostics is not None:
                                self.diagnostics.event(
                                    'smart_turn_checked', complete=turn_complete,
                                    probability=prediction.probability,
                                    inference_ms=prediction.inference_ms)
                        except Exception as error:
                            # A semantic guard failure cannot lose the ordinary
                            # transcription or override the 2 s silence fallback.
                            if self.diagnostics is not None:
                                self.diagnostics.event('smart_turn_error',
                                                       error=type(error).__name__)
                    capture_prefix = os.environ.get('MALBUT_RETEST_RECORD_PREFIX')
                    capture_path = None
                    if capture_prefix:
                        capture_path = Path(f'{capture_prefix}_asr_{kind}_{monotonic():.6f}')
                        Path(str(capture_path) + '.s16').write_bytes(
                            bytes(pcm.pcm if isinstance(pcm, _StreamInput) else pcm))
                    if isinstance(pcm, _StreamInput):
                        window = ({'audio_start_s': pcm.audio_start_s}
                                  if pcm.audio_start_s else {})
                        text = pcm.stream.transcribe(
                            pcm.pcm, 16000, final=pcm.final,
                            speech_end_s=pcm.speech_end_s, **window)
                        retained_start_s = getattr(pcm.stream, 'retained_start_s', 0.0)
                    else:
                        engine = (self.wake if kind == 'wake' else
                                  getattr(pcm, 'transcriber', None) or self.transcriber)
                        text = engine.transcribe(pcm, 16000)
                    if capture_path is not None:
                        Path(str(capture_path) + '.json').write_text(json.dumps({
                            'kind': kind, 'generation': generation, 'uid': uid,
                            'sample_rate': 16000, 'format': 'signed little-endian int16',
                            'text': text,
                        }, ensure_ascii=False, indent=2))
                    wake_command = kind == 'wake' and bool(parse_wake_phrase(text))
                    if wake_command and self.validate_wake_command is not None:
                        self.validate_wake_command(pcm)
                    rejection = (_short_speech_rejection(text, getattr(pcm, 'voiced_s', None))
                                 if self.short_speech_guard and (kind != 'wake' or wake_command)
                                 else None)
                    if rejection is not None:
                        if self.diagnostics is not None:
                            self.diagnostics.event(
                                'recognition_rejected', text=text,
                                utterance_id=uid[0] if isinstance(uid, tuple) else uid,
                                **rejection)
                        text = None
                        error_name = 'InsufficientSpeechEvidence'
                        error_detail = rejection['reason']
                except Exception as error:
                    error_name = type(error).__name__
                    if self.diagnostics is not None:
                        error_detail = str(error)[:2048]
                finally:
                    pcm = None
                    if self.diagnostics is not None:
                        self.diagnostics.event('inference_result', text=text,
                                               error=error_name, error_detail=error_detail)
            result = (kind, generation, uid, text, error_name, retained_start_s)
            if kind == 'endpoint' and self.smart_turn is not None:
                result += (turn_complete,)
            waiting_reported = False
            while not self.stopping.is_set():
                try:
                    self.results.put(result, timeout=0.05)
                    self._trace_job('result_enqueued', kind, generation, uid,
                                    text=text, error=error_name)
                    break
                except Full:
                    if not waiting_reported:
                        self._trace_job('result_queue_wait', kind, generation, uid)
                        waiting_reported = True
                    continue
            else:
                self._trace_job('result_rejected', kind, generation, uid, reason='shutdown')

    @staticmethod
    def _drain(queue, observe=None):
        while True:
            try:
                item = queue.get_nowait()
                if observe is not None:
                    observe(item)
            except Empty:
                return

    def _reset_audio(self):
        self._update_ducking()
        self._audio_generation += 1
        self.command_stream.reset()
        self.wake_stream.reset()
        self._capture_id = None
        self._discard_capture = False
        self._tail_stream = None
        self._endpoint_candidate = None
        self._endpoint_result = None
        self._smart_turn_result = None
        self._stream = None
        self._final_input = None
        self._capture_transcriber = None
        # A finalized utterance may own busy while reusing its in-flight
        # endpoint decode. Cancelling that owner must not block future wakes.
        if self._endpoint_final is not None:
            self._busy = False
        self._endpoint_final = None
        self._drain(self.audio)

    def _terminate(self, reason):
        if self.session.session_id and reason.startswith((
                'utterance_discarded:', 'transcription_queue_full',
                'audio_queue_overflow')):
            self._input_status('failed', '')
        stream = self.command_stream if self.session.active else self.wake_stream
        tail = self._tail_stream
        if tail is None and self._discard_capture and stream.collector.started:
            tail = StreamingUtteranceCollector(stream.is_speech, settings=stream.settings)
            tail.discarding = True
            tail.quiet_frames = stream.collector.silent_frames
        self._retire_session(self.session.session_id)
        self.session.terminate()
        self._generation += 1
        # In-flight inference belongs to the old generation. Keep its worker,
        # but allow a new turn to capture and queue one bounded job behind it.
        self._busy = False
        self._drain(self.jobs, lambda job: self._trace_job(
            'job_discarded', *job[:3], reason=reason))
        self._endpoint_job = None
        self._endpoint_requested_at = None
        self._endpoint_is_partial = False
        cancel = getattr(self.transcriber, 'cancel', None)
        if callable(cancel):
            cancel()
        self._pending = None
        self._reply_request_id = None
        self._barge_reply_playback_id = None
        self._followup_pending = False
        self._followup_deadline = None
        self._retry_notice_deadline = None
        self._command_start_deadline = None
        self._utterance_playback_id = None
        self._reset_audio()
        self._tail_stream = tail
        self.report(reason)

    def poll(self):
        """Advance deadlines and consume bounded work without blocking ROS callbacks."""
        if self.stopping.is_set():
            return
        self._check_aec_health()
        self._expire_web_talk()
        if self.capture_error is None and self._capture_timed_out():
            self.capture_error = RuntimeError('microphone input timeout')
        if self.capture_error is not None:
            self.phase = 'reading_microphone'
            raise self.capture_error
        if self._pending is not None and self.clock() >= self._pending[3]:
            self._terminate('addressee_unknown:timeout')
        if self.overflow.is_set():
            if self._reply_request_id is None:
                self._terminate('audio_queue_overflow')
            else:
                self._reset_audio()
                self.report('audio_queue_overflow')
            self.overflow.clear()
        try:
            result = self.results.get_nowait()
        except Empty:
            result = None
        if result is not None:
            self._trace_job('result_dequeued', *result[:3], text=result[3], error=result[4])
        if result is not None and result[0] in ('endpoint', 'partial'):
            # Release an already acknowledged prefix before buffered capture can
            # fill the PCM budget. Endpoint decisions still wait for that audio:
            # queued resumed speech must invalidate a provisional endpoint.
            _, generation, token, text, error, *retained = result
            if (generation == self._generation and token[0] == self._capture_id
                    and self.session.active and self._usable_text(text, error)):
                self.command_stream.collector.discard_before(retained[0] if retained else 0.0)
        for _ in range(self.audio.maxsize):
            try:
                generation, captured_at, pcm, busy = self.audio.get_nowait()
            except Empty:
                break
            if generation == self._audio_generation:
                self.feed(pcm, captured_at=captured_at, busy_at_capture=busy)
        if result is not None:
            if result[0] in ('endpoint', 'partial'):
                self._accept_endpoint(*result[1:], partial=result[0] == 'partial')
            else:
                if result[1] == self._generation:
                    self._busy = False
                self._accept_result(*result)
        self._submit_endpoint()
        self._finish_ready_endpoint()
        if self.session.tick():
            self._fail_command('session_ended:input_timeout')
        self._expire_command_wait(self.clock())
        if (self._retry_notice_deadline is not None
                and self.clock() >= self._retry_notice_deadline):
            self._terminate('retry_notice_timeout')

    def _check_aec_health(self):
        """Close duplex capture before allowing a failed AEC recorder's raw PCM."""
        if not self.input_has_aec or getattr(self.recorder, 'aec_ready', True):
            return
        self.input_has_aec = False
        self.barge_in_mode = 'legacy'
        self.barge_in_ducking = False
        if self._ducked_playback is not None:
            self.publish_control(self._ducked_playback, 'unduck')
            self._retire_duck()
        self._barge_reply_playback_id = None
        self._raw_playback_gate = self.session.playback_state == 'playing'
        # XFM has an internal capture delay around half a second. Include that
        # tail when switching from filtered to raw audio.
        self._raw_tail_s = 1.0
        self._raw_gate_until = self.clock() + self._raw_tail_s
        request = self._reply_request_id
        followup = self._followup_pending
        retry_deadline = self._retry_notice_deadline
        if self.session.session_id:
            self._input_status('failed', '')
        self._terminate('aec_input_reset')
        self._reply_request_id = request
        self._followup_pending = followup
        self._retry_notice_deadline = retry_deadline
        self.report('aec_fallback:half_duplex:' + str(getattr(self.recorder, 'failure', 'unavailable')))
        acknowledge = getattr(self.recorder, 'acknowledge_half_duplex', None)
        if callable(acknowledge):
            acknowledge()

    def _expire_command_wait(self, captured_at):
        if (self._followup_deadline is not None
                and captured_at >= self._followup_deadline):
            # A quiet conversation ending is not a failed recognition attempt.
            self._terminate('session_ended:followup_timeout')
            self.report('waiting_for_wake')
            return True
        if (self._command_start_deadline is not None
                and captured_at >= self._command_start_deadline):
            self._fail_command('session_ended:input_timeout')
            return True
        return False

    def _input_blocked(self, captured_at):
        return (self.input_has_aec and not getattr(self.recorder, 'aec_ready', True)) or (self._web_talk_blocked(captured_at) or (
                    self._reply_request_id is not None and not self._can_barge_into_reply())
                or self._chime_playing or captured_at < self._chime_gate_until) or (
            not self.input_has_aec and (
                self._raw_playback_gate or captured_at < self._raw_gate_until
            )
        )

    def _can_barge_into_reply(self):
        """Only an acknowledged ordinary final reply opens duplex capture."""
        return (self.barge_in_mode == 'reachy' and self.input_has_aec
                and self._reply_request_id is not None
                and self._barge_reply_playback_id is not None
                and self._barge_reply_playback_id == self.session.playback_id
                and self.session.playback_state == 'playing'
                and self.session.active and not self.session.session_id)

    def _publish_control(self, playback_id, command):
        if command in ('pause', 'stop') and playback_id == self._ducked_playback:
            # Pause restores gain in the player; never unduck ahead of a pause.
            self._retire_duck()
        if command == 'pause' and (
            not self.input_has_aec
            or self._pending is not None and self._pending[1] != playback_id
        ):
            return
        self.publish_control(playback_id, command)

    def _retire_duck(self, *, discard_candidate=False):
        if discard_candidate and not self.command_stream.collector.started:
            self.command_stream.collector.discard_candidate()
        self._ducked_playback = None
        self._duck_clear_after = None
        self._duck_lease_until = None
        self._duck_cycle += 1

    def duck_control_token(self, playback_id):
        return self._duck_cycle if playback_id == self._ducked_playback else None

    def on_duck_control(self, playback_id, token, accepted, *, issued_at=None):
        """Only this gain cycle's timely successful result can arm confirmation."""
        if (token is None or token != self._duck_cycle
                or playback_id != self._ducked_playback
                or not self._can_barge_into_reply()):
            return
        now = self.clock()
        if (not accepted or self._duck_lease_until is not None
                and now >= self._duck_lease_until):
            self._retire_duck(discard_candidate=True)
            return
        # Request issuance precedes server acceptance, so this is a conservative
        # lower bound on the player's 300 ms expiry, never ACK arrival + 300 ms.
        if issued_at is None or now >= issued_at + .3:
            return
        self._duck_lease_until = max(self._duck_lease_until or 0.0, issued_at + .3)
        if self._duck_clear_after is None:
            # Experimental output-settling allowance, independent of the gap hold.
            self._duck_clear_after = now + .128
            self.report('barge_duck_armed:' + json.dumps(dict(
                playback_id=playback_id, cycle=token, issued_at=issued_at,
                acknowledged_at=now, lease_until=self._duck_lease_until,
                clear_after=self._duck_clear_after, evidence_margin_s=.152)))

    def _barge_evidence_before(self, pcm):
        timestamp = getattr(pcm, 'source_timestamp', None)
        # Pulse virtual-source time is not an ADC hardware timestamp. Bound the
        # read span (normally32) + bridge24 + VAD32 + window/cache/context64 ms.
        return None if timestamp is None else timestamp - max(.032, len(pcm) / 32000) - .120

    def _barge_start_allowed(self, pcm):
        if not self._can_barge_into_reply():
            return True
        evidence_before = self._barge_evidence_before(pcm)
        return (self._ducked_playback == self.session.playback_id
                and self._duck_lease_until is not None
                and self.clock() < self._duck_lease_until
                and self._duck_clear_after is not None and evidence_before is not None
                and evidence_before >= self._duck_clear_after)

    def _update_ducking(self, candidate=False, *, hold_gap=False, fresh_qualified=False):
        """Use the existing Silero onset candidate, without another VAD/filter."""
        pid = self.session.playback_id
        eligible = (self.barge_in_ducking and self.input_has_aec
                    and self.session.active and not self.session.session_id
                    and self.session.playback_state == 'playing'
                    and self.session.utterance_id is None)
        if eligible and fresh_qualified and self._can_barge_into_reply():
            collector = self.command_stream.collector
            quiet_s = (collector.wait_frames - collector.candidate_last_voiced_frame) * .02
            self._duck_hold_until = self.clock() - quiet_s + .25
        desired = pid if eligible and candidate else None
        if (hold_gap and eligible and self._ducked_playback == pid
                and self.clock() < self._duck_hold_until):
            desired = pid
        if self._ducked_playback is not None and self._ducked_playback != desired:
            self.publish_control(self._ducked_playback, 'unduck')
            self._retire_duck(discard_candidate=self._can_barge_into_reply())
        if (desired is not None and self._duck_lease_until is not None
                and self.clock() >= self._duck_lease_until):
            self._retire_duck(discard_candidate=True)
        if desired is not None and (self._ducked_playback is None
                                    or self.clock() >= self._duck_renew_at):
            if self._ducked_playback is None:
                self._duck_cycle += 1
                self._duck_clear_after = None
                self._duck_lease_until = None
                self._ducked_playback = desired
                self._duck_hold_until = self.clock() + (.25 if self.barge_in_mode == 'reachy' else 0.0)
            # Renewal alone extends neither the candidate gap nor source clearance.
            self.publish_control(desired, 'duck')
            self._duck_renew_at = self.clock() + .15

    def feed(self, pcm, *, captured_at=None, busy_at_capture=False):
        """Consume captured PCM on the owner thread; the microphone remains open."""
        self._expire_web_talk()
        captured_at = self.clock() if captured_at is None else captured_at
        if self.stopping.is_set() or self._input_blocked(captured_at):
            return
        self._event_time = captured_at
        try:
            self._feed(pcm, busy_at_capture)
        finally:
            self._event_time = None

    def _feed(self, pcm, busy_at_capture):
        # AEC3's nonlinear suppression can erase an overlapping human syllable.
        # Keep the aligned microphone for the optional robust decoder;
        # the conservative AEC mask still owns every onset/stop decision.
        recovery_pcm = getattr(pcm, 'recovery_pcm', None)
        if (recovery_pcm is not None and self.interruption_transcriber is not None
                and (self._can_barge_into_reply() or self._capture_transcriber is not None)):
            pcm = CapturePCM(recovery_pcm, pcm.vad_pcm,
                             source_timestamp=getattr(pcm, 'source_timestamp', None))
        if self._expire_command_wait(self._event_time):
            return
        if self.session.tick():
            self._fail_command('session_ended:input_timeout')
            return
        if self._tail_stream is not None:
            self._tail_stream.feed(pcm)
            if not self._tail_stream.discarding:
                self._tail_stream = None
            return
        stream = self.command_stream if self.session.active else self.wake_stream
        start_blocked = busy_at_capture or self._busy or self._pending is not None
        if (self._ducked_playback is not None and self._duck_lease_until is not None
                and self.clock() >= self._duck_lease_until):
            self._retire_duck(discard_candidate=True)
        previous_collector = stream.collector
        previous_voice = previous_collector.candidate_last_voiced_frame
        hold_qualified = (not start_blocked and self._can_barge_into_reply()
                          and self._ducked_playback == self.session.playback_id
                          and self._duck_lease_until is not None
                          and self.clock() < self._duck_lease_until)
        for event in stream.feed(pcm, start_blocked=start_blocked,
                                 can_start=self._barge_start_allowed(pcm),
                                 hold_qualified=hold_qualified):
            if event.status == 'speech_started':
                self._discard_capture = (event.start_blocked or self._busy
                                         or self._pending is not None)
                if self._discard_capture:
                    self.report('speech_discarded:busy')
                    continue
                if self.session.active:
                    self._command_start_deadline = None
                    self._followup_deadline = None
                    interrupt_reply = self._can_barge_into_reply()
                    if interrupt_reply:
                        self.report('barge_duck_source:' + json.dumps(dict(
                            playback_id=self._ducked_playback, cycle=self._duck_cycle,
                            source_timestamp=getattr(pcm, 'source_timestamp', None),
                            evidence_before=self._barge_evidence_before(pcm),
                            lease_until=self._duck_lease_until,
                            clear_after=self._duck_clear_after, confirmed_at=self.clock())))
                        # Detach the old request before STOP can acknowledge. Its
                        # terminal event must never discard the new utterance.
                        self._reply_request_id = None
                        self._barge_reply_playback_id = None
                        self._followup_pending = False
                    self._capture_id = self.session.user_speech_started(
                        stop_playback=interrupt_reply)
                    if interrupt_reply:
                        self.report('barge_in_confirmed:reachy')
                    self._capture_transcriber = (
                        self.interruption_transcriber if interrupt_reply else None)
                    self._input_status('started', self._capture_id)
                    self._endpoint_result = None
                    self._stream = (self._stream_factory() if callable(self._stream_factory)
                                    and self.command_stream.partial_interval_s is not None
                                    and self._capture_transcriber is None
                                    else None)
                    self._utterance_playback_id = self.session.interrupted_playback_id
                    self.report('speech_started')
            elif event.status in ('endpoint_check', 'partial_check'):
                if event.status == 'partial_check' and self._stream is None:
                    continue
                if not self._discard_capture and self._capture_id is not None:
                    self._endpoint_candidate = (self._capture_id, event)
                    self._submit_endpoint()
            elif event.status in ('complete', 'too_long', 'buffer_overflow'):
                if self._discard_capture:
                    self._discard_capture = False
                    continue
                if event.status in ('too_long', 'buffer_overflow'):
                    if self.session.active:
                        self._terminate('utterance_discarded:' + event.status)
                        stream.discarding = True
                        self._tail_stream = stream
                    else:
                        self.report('wake_too_long')
                    continue
                kind = 'command' if self.session.active else 'wake'
                audio_generation = self._audio_generation
                self._complete_capture(kind, event)
                if audio_generation != self._audio_generation:
                    # The chime discarded this chunk's remaining capture events.
                    break
        collector = stream.collector
        fresh_voice = (collector.candidate_last_voiced_frame > (
            previous_voice if collector is previous_collector else 0))
        candidate = (not start_blocked and not collector.started
                     and bool(collector.candidate_audio)
                     # Two positive Silero windows can cover four 20 ms frames.
                     # Require sustained evidence before the first gain change.
                     and (not self._can_barge_into_reply()
                          or self._ducked_playback is not None
                          or collector.candidate_frames >= 5))
        self._update_ducking(candidate=(candidate and (
            fresh_voice or not self._can_barge_into_reply())), hold_gap=True,
            fresh_qualified=(candidate and fresh_voice and collector.candidate_frames
                             >= math.ceil(stream.settings.min_speech_s / .02)))

    def _submit_endpoint(self):
        if (self._endpoint_candidate is None or self._endpoint_job is not None
                or self._busy or self._discard_capture):
            return
        uid, event = self._endpoint_candidate
        collector = self.command_stream.collector
        partial = event.status == 'partial_check'
        if (uid != self._capture_id or not collector.started
                or (not partial
                    and not self.command_stream.endpoint_is_current(event.revision))):
            self._endpoint_candidate = None
            return
        if partial:
            # During a pause, wait for the endpoint snapshot instead of
            # starting another preview that would delay its fresher audio.
            if collector.silent_frames:
                self._endpoint_candidate = None
                return
            # Replace an older pending preview with all audio collected so far.
            event = collector.snapshot('partial_check')
        key = (self._generation, uid, event.revision)
        if (self._endpoint_result is not None and self._endpoint_result[0] == key
                and self._usable_text(*self._endpoint_result[1:])):
            self._endpoint_candidate = None
            return
        # Match the fallback input duration without extending observed silence.
        target_frames = math.ceil(self.command_stream.settings.silence_timeout_s / 0.02)
        observed_frames = round(event.silence_s / 0.02)
        padding_frames = max(0, target_frames - observed_frames)
        pcm = event.pcm + bytes(padding_frames * 640)
        try:
            kind = 'partial' if partial else 'endpoint'
            payload = self._inference_input(
                event.pcm if self._stream is not None else pcm, silence_s=event.silence_s,
                audio_start_s=event.audio_start_s, voiced_s=event.voiced_s,
                endpoint_pcm=(event.pcm[-256000:]
                              if self.smart_turn is not None and not partial else None))
            self._enqueue_job((kind, key[0], (uid, event.revision), payload))
        except Full:
            return
        self._endpoint_job = key
        self._endpoint_is_partial = partial
        self._endpoint_requested_at = self.clock()
        self._endpoint_candidate = None
        self.report('partial_started' if partial else
                    f'checking_endpoint:silence_s={event.silence_s:.2f}')

    @staticmethod
    def _usable_text(text, error):
        return error is None and isinstance(text, str) and bool(text.strip())

    def _inference_input(self, pcm, *, final=False, silence_s=0.0, audio_start_s=0.0,
                         voiced_s=None, endpoint_pcm=None):
        if self._stream is None:
            return _SpeechPCM(pcm, voiced_s, endpoint_pcm, self._capture_transcriber)
        speech_end_s = audio_start_s + max(0.0, len(pcm) / 32000 - silence_s)
        return _StreamInput(pcm, self._stream, final, speech_end_s, audio_start_s,
                            voiced_s, endpoint_pcm)

    def _complete_capture(self, kind, event):
        uid = self._capture_id
        key = (self._generation, uid, event.revision)
        self._capture_id = None
        self._endpoint_candidate = None
        self._busy = True
        self.report('transcribing' if kind == 'command' else 'recognizing_wake')
        if kind == 'command':
            self.report(f'endpoint_finalized:silence_s={event.silence_s:.2f}')
            audio_at = self.clock() if self._event_time is None else self._event_time
            self._timing('endpoint', uid,
                         estimated_speech_end_s=audio_at - event.silence_s)
            self._play_endpoint_chime()
            if (self._endpoint_result is not None and self._endpoint_result[0] == key
                    and self._usable_text(*self._endpoint_result[1:])):
                _, text, error = self._endpoint_result
                self._endpoint_result = None
                self._busy = False
                self._trace_job('endpoint_reused', 'endpoint', key[0], (uid, event.revision),
                                reason='cached_result')
                self._accept_result('command', key[0], uid, text, error)
                return
            if self._endpoint_job == key and not self._endpoint_is_partial:
                self._trace_job('endpoint_wait', 'endpoint', key[0], (uid, event.revision),
                                reason='in_flight_result')
                self._endpoint_final = key
                self._final_input = self._inference_input(
                    event.pcm, final=True, silence_s=event.silence_s,
                    audio_start_s=event.audio_start_s, voiced_s=event.voiced_s)
                return
        # A resumed utterance can finish before its obsolete candidate is taken
        # by the worker. Replace only that queued candidate with the final audio.
        if self.jobs.full():
            try:
                stale = self.jobs.get_nowait()
            except Empty:
                pass
            else:
                self._trace_job('job_discarded', *stale[:3], reason='final_capture_replaced_job')
                if stale[0] not in ('endpoint', 'partial'):
                    self._terminate('transcription_queue_full')
                    return
                if self._endpoint_job == (stale[1], *stale[2]):
                    self._endpoint_job = None
        payload = (self._inference_input(event.pcm, final=True, silence_s=event.silence_s,
                                         audio_start_s=event.audio_start_s,
                                         voiced_s=event.voiced_s)
                   if kind == 'command' else _SpeechPCM(event.pcm, event.voiced_s))
        self._enqueue_job((kind, self._generation, uid, payload))

    def _fail_command(self, reason):
        """End an ordinary failed turn locally, without an Agent retry notice."""
        # Close the generation before playing: duplicate and late inference can
        # neither publish text nor repeat this cue. Gate even AEC capture.
        self._chime_playing = True
        try:
            self._terminate(reason)
            if self.on_failure is None:
                self.report('failure_chime_unavailable')
            else:
                try:
                    self.on_failure()
                except Exception as error:
                    self.report('failure_chime_failed:' + type(error).__name__)
        finally:
            self._chime_gate_until = self.clock() + self.chime_tail_s
            self._reset_audio()
            self._chime_playing = False
        self.report('waiting_for_wake')

    def _play_endpoint_chime(self):
        """Acknowledge capture completion without resetting its pending inference."""
        if self.on_endpoint is None or self.session.playback_state in ('playing', 'paused'):
            return
        self._chime_playing = True
        self._audio_generation += 1
        self._drain(self.audio)
        try:
            self.on_endpoint()
        except Exception as error:
            self.report('endpoint_chime_failed:' + type(error).__name__)
        finally:
            self._chime_gate_until = self.clock() + self.chime_tail_s
            self._audio_generation += 1
            # Keep the completed PCM, stream, generation and endpoint job alive.
            # Only unfinished capture fragments and speaker echo are discarded.
            self.command_stream.reset()
            self._drain(self.audio)
            self._chime_playing = False

    def _accept_endpoint(self, generation, token, text, error_name, retained_start_s=0.0,
                         turn_complete=None, *, partial=False):
        uid, revision = token
        kind = 'partial' if partial else 'endpoint'
        key = (generation, uid, revision)
        if self._endpoint_job == key:
            self._endpoint_job = None
            elapsed = max(0.0, self.clock() - self._endpoint_requested_at)
            self._endpoint_requested_at = None
            self.report(f'endpoint_checked:wait_s={elapsed:.3f}')
        if generation != self._generation:
            self._trace_job('result_rejected', kind, generation, token, reason='stale_generation')
            return
        if self._endpoint_final == key:
            self._endpoint_final = None
            payload, self._final_input = self._final_input, None
            if self._usable_text(text, error_name):
                self._busy = False
                self._trace_job('endpoint_reused', kind, generation, token,
                                reason='final_capture_waiting')
                self._accept_result('command', generation, uid, text, error_name)
            else:
                self._trace_job('result_rejected', kind, generation, token,
                                reason='unusable_endpoint_retry_final', error=error_name)
                self.report('partial_failed')
                self._enqueue_job(('command', generation, uid, payload))
            return
        if uid != self._capture_id or not self.session.active:
            self._trace_job('result_rejected', kind, generation, token,
                            reason='inactive_or_different_capture')
            return
        if self._usable_text(text, error_name):
            self._trace_job('preview_accepted', kind, generation, token, text=text)
            # Apply only to this live utterance. Its VAD revision may advance
            # during inference; the acknowledged prefix still remains valid.
            self.command_stream.collector.discard_before(retained_start_s)
            self.report('partial_ready')
            if self.on_partial is not None:
                self.on_partial(uid, text)
        else:
            self._trace_job('result_rejected', kind, generation, token,
                            reason='unusable_preview', error=error_name)
            self.report('partial_failed')
        # Speech-time previews do not cover quiet/weak trailing syllables yet.
        # Only an endpoint snapshot can supply a final result, even if VAD's
        # last voiced revision did not change while that tail was recorded.
        if partial or revision != self.command_stream.collector.revision:
            self._trace_job('endpoint_not_cached', kind, generation, token,
                            reason='preview_only' if partial else 'speech_revision_changed')
            return
        self._endpoint_result = (key, text, error_name)
        self._smart_turn_result = (key, turn_complete is True)
        if self.smart_turn is not None:
            self.report('smart_turn_complete' if turn_complete is True else
                        'smart_turn_incomplete' if turn_complete is False else
                        'smart_turn_unavailable')
        self._trace_job('endpoint_cached', kind, generation, token,
                        usable=self._usable_text(text, error_name))
        self._finish_ready_endpoint()

    def _finish_ready_endpoint(self):
        """Reuse early inference only after 1.0 seconds of current observed silence."""
        if self._endpoint_result is None:
            return
        (generation, uid, revision), text, error_name = self._endpoint_result
        if (generation != self._generation or uid != self._capture_id
                or not self.session.active
                or not self.command_stream.collector.started
                or self.command_stream.collector.revision != revision):
            self._endpoint_result = None
            self._trace_job('endpoint_cache_discarded', 'endpoint', generation, (uid, revision),
                            reason='capture_or_revision_changed')
            return
        if (self.command_stream.collector.silent_frames * 0.02 >= 1.0
                and error_name is None and is_complete_korean_utterance(text)
                and (self.smart_turn is None or self._smart_turn_result == (
                    (generation, uid, revision), True))):
            event = self.command_stream.finish_endpoint(revision)
            if event is not None:
                self._complete_capture('command', event)

    def _accept_result(self, kind, generation, uid, text, error_name, retained_start_s=0.0):
        if generation != self._generation:
            self._trace_job('result_rejected', kind, generation, uid, reason='stale_generation')
            return
        if kind != 'wake' and (not self.session.active or uid != self.session.utterance_id):
            self._trace_job('result_rejected', kind, generation, uid,
                            reason='inactive_or_different_utterance')
            return
        failure = (
            'transcription_failed:' + error_name if error_name is not None
            else 'empty_transcript' if not isinstance(text, str) or not text.strip()
            else None
        )
        if failure is not None:
            self._trace_job('result_rejected', kind, generation, uid, reason=failure)
            if kind == 'wake':
                self._terminate(failure)
            elif not self.session.session_id:
                self._fail_command(failure)
            else:
                # Use the existing utterance/session failure distinction:
                # a rejected candidate is retryable; an operational fault
                # invalidates the whole confirmation session (empty ID).
                discarded = error_name in ('SileroNonSpeech', 'InsufficientSpeechEvidence') or (
                    error_name is None and isinstance(text, str) and not text.strip())
                self._input_status('failed', uid if discarded else '')
                self.session.discard_utterance(uid)
                self._utterance_playback_id = None
                self.report('confirmation_input_discarded:' + failure if discarded else failure)
            return
        if kind == 'wake':
            command = parse_wake_phrase(text)
            if command is not None:
                self._trace_job('result_accepted', kind, generation, uid, reason='wake_match')
                self.session.activate()
                self._reset_audio()
                self.report('wake_detected')
                if command:
                    # This utterance already contains the request. Reuse its local
                    # transcript, and enter the same once-only reply/follow-up path.
                    uid = self.session.user_speech_started()
                    self._input_status('started', uid)
                    self._utterance_playback_id = self.session.interrupted_playback_id
                    self.report('wake_command_detected')
                    self._play_endpoint_chime()
                    self._accept_result('command', generation, uid, command, None)
                    return
                if self.on_wake is None:
                    self.report('wake_chime_unavailable')
                else:
                    # Never feed the acknowledgement or queued speaker tail to ASR,
                    # including with AEC enabled on the microphone.
                    self._chime_playing = True
                    try:
                        self.on_wake()
                    except Exception as error:
                        self._terminate('wake_chime_failed:' + type(error).__name__)
                    finally:
                        self._chime_gate_until = self.clock() + self.chime_tail_s
                        self._reset_audio()
                        self._chime_playing = False
                if self.session.active:
                    self._command_start_deadline = (
                        max(self.clock(), self._chime_gate_until)
                        + self.command_stream.settings.start_timeout_s)
                    if self.diagnostics is not None:
                        self.report('wake_input_ready')
            else:
                self._trace_job('result_rejected', kind, generation, uid, reason='not_wake')
                self.report('not_wake')
            return
        pid = self._utterance_playback_id
        if pid is not None:
            if pid != self.session.playback_id:
                self._trace_job('result_rejected', kind, generation, uid, reason='stale_playback')
                self._terminate('addressee_unknown:stale_playback')
                return
            self._pending = (uid, pid, text, self.clock() + 45.0)
            self._trace_job('result_held', kind, generation, uid, reason='awaiting_addressee')
            self.report('awaiting_addressee:timeout_s=45')
            self.publish_interruption(uid, pid, text)
        else:
            self._trace_job('result_accepted', kind, generation, uid, reason='final_utterance')
            self.session.finish_utterance(uid, text, addressed=True)
        self._utterance_playback_id = None

    def on_playback_status(self, playback_id, state, *, interim=False, request_id=''):
        """Track acknowledged playback and discard raw echo on gate transitions."""
        if self.stopping.is_set():
            return
        self.poll()
        if (self._web_talk_quiet and state in ('playing', 'paused')
                and isinstance(playback_id, str) and playback_id.strip()):
            # A request that raced the web talk start is stopped as soon as it sounds.
            self._stop_speech(playback_id)
        previous = (self.session.playback_id, self.session.playback_state)
        self.session.on_playback_status(playback_id, state, interim=interim)
        if (self.barge_in_mode == 'reachy' and self.input_has_aec
                and self._reply_request_id is not None
                and request_id == self._reply_request_id and not interim
                and state == 'playing' and self.session.playback_id == playback_id
                and self.session.playback_state == 'playing'
                and not self.session.session_id and not self._web_talk_blocked(self.clock())
                and self._barge_reply_playback_id != playback_id):
            self._reset_audio()
            self._barge_reply_playback_id = playback_id
            self.session.activate()
            self.report('barge_in_ready:reachy')
        if self._followup_deadline is not None:
            # Only the correlated reply starts this wait, not unrelated playback.
            self.session.deadline = None
        current = (self.session.playback_id, self.session.playback_state)
        if previous != current and not self.input_has_aec:
            if current[1] in ('playing', 'paused') and self.session.utterance_id is not None:
                self._terminate('utterance_discarded:playback_without_aec')
            self._raw_playback_gate = current[1] == 'playing'
            # This drain guard limits queued playback tails; it is not AEC.
            self._raw_gate_until = self.clock() + self._raw_tail_s
            self._reset_audio()
            if self._raw_playback_gate:
                self.report('barge_in_requires_aec')
        if self.session.interrupted_playback_id is not None:
            self._utterance_playback_id = self.session.interrupted_playback_id
        if self._pending is not None and self._pending[1] != self.session.playback_id:
            self._terminate('addressee_unknown:stale_playback')
        if (self._reply_request_id is not None and request_id == self._reply_request_id
                and not interim and state in ('finished', 'failed', 'stopped')
                and (self._barge_reply_playback_id is None
                     or playback_id == self._barge_reply_playback_id)):
            # Failed synthesis can terminate before PLAYING; progress cannot unlock input.
            followup = (state == 'finished' and self._followup_pending
                        and not self._web_talk_blocked(self.clock()))
            keep_onset = (followup and self.barge_in_mode == 'reachy'
                          and self.input_has_aec and self.session.active
                          and not self.session.session_id
                          and self._barge_reply_playback_id == playback_id
                          and self.session.playback_id == playback_id
                          and not self._busy and self._pending is None
                          and not self._chime_playing
                          and self.clock() >= self._chime_gate_until)
            if keep_onset:
                # A weak first word can still be in pre-roll before VAD qualifies.
                # Preserve bounded capture when its owning reply finishes.
                self._reply_request_id = None
                self._barge_reply_playback_id = None
                self._followup_pending = False
                self._retry_notice_deadline = None
                self.session.deadline = None
                self._update_ducking()
            else:
                self._terminate('reply_finished' if followup else 'waiting_for_wake')
            if followup:
                self.session.activate()
                self._followup_deadline = self.clock() + self.followup_timeout_s
                self.report('waiting_for_followup')

    def on_addressee(self, utterance_id, playback_id, decision):
        """Accept exactly one matching decision before the held candidate expires."""
        if self.stopping.is_set() or self._pending is None:
            return
        uid, pid, text, deadline = self._pending
        if (utterance_id, playback_id) != (uid, pid):
            return
        if self.clock() >= deadline:
            self._terminate('addressee_unknown:timeout')
            return
        if decision == 'unknown':
            self._terminate('addressee_unknown:agent')
        elif decision in ('addressed', 'not_addressed'):
            self._pending = None
            self.session.finish_utterance(uid, text, addressed=decision == 'addressed')
        else:
            self.report('invalid_addressee_decision')

    def close(self):
        """Stop capture before deletion and suppress any late local inference result."""
        if self._closed:
            return
        self._update_ducking()
        self._closed = True
        self.stopping.set()
        cancel = getattr(self.transcriber, 'cancel', None)
        if callable(cancel):
            cancel()
        self.session.terminate()
        self._pending = None
        failure = None
        try:
            if self.recorder is not None and self._started:
                self.recorder.stop()
        except Exception as error:
            failure = error
        finally:
            self._started = False
        if self.capture_thread is not None:
            self.capture_thread.join(timeout=2.0)
        if self.recorder is not None:
            if self.capture_thread is None or not self.capture_thread.is_alive():
                recorder, self.recorder = self.recorder, None
                try:
                    recorder.delete()
                except Exception as error:
                    failure = failure or error
            else:
                self.report('capture_shutdown_pending')
        if self.asr_thread is not None:
            self.asr_thread.join(timeout=2.0)
            if self.asr_thread.is_alive():
                self.report('asr_shutdown_pending')
        self._drain(self.audio)
        self._drain(self.jobs, lambda job: self._trace_job(
            'job_discarded', *job[:3], reason='shutdown'))
        self._drain(self.results, lambda result: self._trace_job(
            'result_rejected', *result[:3], reason='shutdown'))
        self._endpoint_job = None
        self._endpoint_requested_at = None
        self._endpoint_candidate = None
        self._endpoint_result = None
        self._endpoint_final = None
        self._final_input = None
        self._stream = None
        if failure is not None:
            raise failure
