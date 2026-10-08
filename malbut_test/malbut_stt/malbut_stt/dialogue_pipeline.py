"""Keep continuous capture and local inference outside serialized dialogue events."""

import math
import json
from collections import OrderedDict
from contextlib import nullcontext
from dataclasses import dataclass
from queue import Empty, Full, Queue
from threading import Event, Thread
from time import monotonic
from uuid import uuid4

from malbut_stt.audio import CaptureSettings, MicrophoneOverflow
from malbut_stt.conversation import ConversationSession
from malbut_stt.endpoint import is_complete_korean_utterance
from malbut_stt.pipeline import pcm_bytes
from malbut_stt.streaming import StreamingUtteranceCollector
from malbut_stt.wake import split_wake_command


MAX_RETIRED_SESSION_IDS = 256
MAX_RETIRED_REQUEST_IDS = 256
RETIRED_REQUEST_TTL_SECONDS = 300.0
MICROPHONE_TIMEOUT_SECONDS = 5.0
RETRY_NOTICE_TIMEOUT_SECONDS = 45.0


@dataclass(frozen=True)
class _StreamInput:
    pcm: bytes
    stream: object
    final: bool
    speech_end_s: float
    audio_start_s: float = 0.0


class DialoguePipeline:
    """Run one capture thread and one ASR worker; poll and callbacks share one owner.

    One utterance may await inference or addressee classification. Additional
    utterances are explicitly discarded through their endpoint while it is busy.
    An accepted ordinary request blocks capture until its final reply terminates;
    the next request requires a new wake. Agent-owned sessions keep their policy.
    ``input_has_aec`` asserts that the selected microphone already supplies AEC;
    this class does not remove playback echo from raw microphone audio.
    """

    def __init__(self, *, recorder_factory, wake, transcriber, is_speech,
                 publish_transcript, publish_control, publish_interruption,
                 report, settings=None, input_has_aec=False, clock=monotonic,
                 on_wake=None, endpoint_predecode_s: float | None = None,
                 partial_interval_s: float | None = 2.0, on_partial=None,
                 on_endpoint=None, publish_input_status=None, diagnostics=None,
                 stop_speech=None, on_failure=None, on_lifecycle=None,
                 cancel_request=None, receipt_timeout_s=5.0, reply_timeout_s=120.0,
                 inference_timeout_s=35.0):
        for name, value in (('receipt_timeout_s', receipt_timeout_s),
                            ('reply_timeout_s', reply_timeout_s),
                            ('inference_timeout_s', inference_timeout_s)):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(name + ' must be finite and positive')
        self.recorder_factory = recorder_factory
        self.wake = wake
        self.transcriber = transcriber
        self.publish_transcript = publish_transcript
        self.publish_interruption = publish_interruption
        self.publish_control = publish_control
        # stop_speech(playback_id) drops Agent speech; an empty ID drops all of it.
        self.stop_speech = stop_speech
        self.cancel_request = cancel_request
        self.on_lifecycle = on_lifecycle
        self.receipt_timeout_s = receipt_timeout_s
        self.reply_timeout_s = reply_timeout_s
        self.inference_timeout_s = inference_timeout_s
        self._report = report
        self.diagnostics = diagnostics
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
        if endpoint_predecode_s is not None and (
            isinstance(endpoint_predecode_s, bool)
            or not math.isfinite(endpoint_predecode_s)
            or not 0 < endpoint_predecode_s <= 1.0 < settings.silence_timeout_s
        ):
            raise ValueError('predecode must start by 1.0 seconds before the fallback')
        self.command_stream = StreamingUtteranceCollector(
            is_speech, settings=settings,
            early_endpoint_s=(endpoint_predecode_s if endpoint_predecode_s is not None
                              else 1.0 if settings.silence_timeout_s > 1.0 else None),
            partial_interval_s=(partial_interval_s if callable(self._stream_factory) else None),
        )
        self.wake_stream = StreamingUtteranceCollector(
            is_speech, settings=CaptureSettings(silence_timeout_s=0.4, max_utterance_s=6.0),
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
        self._shutdown_prepared = False
        self._generation = 0
        self._audio_generation = 0
        self._capture_open_generation = None
        self._busy = False
        self._capture_id = None
        self._discard_capture = False
        self._utterance_playback_id = None
        self._pending = None
        self._reply_request_id = None
        self._reply_receipt_deadline = None
        self._reply_deadline = None
        self._reply_playback_ids = set()
        self._retired_request_ids = OrderedDict()
        self._quiescent_request_ids = OrderedDict()
        self._obsolete_playback_ids = {}
        self._playback_request_id = None
        self._utterance_lifecycle = None
        self._last_gate_reason = None
        self._wake_ready_pending = False
        self._inference_started_at = None
        self._retry_notice_deadline = None
        self._command_start_deadline = None
        self._web_talk_lease_id = None
        self._web_talk_deadline = 0.0
        self._web_talk_drain_until = 0.0
        self._web_talk_quiet = False
        self._retired_session_ids = OrderedDict()
        self._raw_playback_gate = False
        self._raw_gate_until = 0.0
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
                command_start_deadline=self._command_start_deadline)
            if self.diagnostics.error and not self._diagnostic_error_reported:
                self._diagnostic_error_reported = True
                self._report('diagnostics_stopped')

    def _lifecycle(self, event, **metadata):
        """Emit identifiers and outcomes only, independently of Agent input events."""
        record = dict(event=event, clock=self.clock(), generation=self._generation, **metadata)
        self.report('speech_lifecycle:' + json.dumps(record, sort_keys=True))
        if self.on_lifecycle is not None:
            try:
                self.on_lifecycle(record)
            except Exception as error:
                self._report('lifecycle_diagnostic_failed:' + type(error).__name__)

    def _start_utterance_lifecycle(self, uid, *, source='microphone'):
        self._utterance_lifecycle = (uid, self.session.session_id, self._generation)
        self._lifecycle('utterance_started', utterance_id=uid,
                        session_id=self.session.session_id, source=source)

    def _finish_utterance_lifecycle(self, reason):
        if self._utterance_lifecycle is not None:
            uid, session_id, generation = self._utterance_lifecycle
            self._utterance_lifecycle = None
            self._lifecycle('utterance_terminal', utterance_id=uid,
                            session_id=session_id, utterance_generation=generation,
                            reason=reason)

    def _request_is_retired(self, request_id):
        return self._request_in_history(self._retired_request_ids, request_id)

    def _request_in_history(self, history, request_id):
        if not isinstance(request_id, str):
            return False
        now = self.clock()
        while history:
            uid, expires = next(iter(history.items()))
            if expires > now:
                break
            history.pop(uid)
        return request_id in history

    def on_request_quiescent(self, request_id):
        """Accept TTS proof that this cancelled request cannot still produce sound."""
        if self.stopping.is_set() or not isinstance(request_id, str) or not request_id.strip():
            return False
        if self._request_in_history(self._quiescent_request_ids, request_id):
            return True
        if request_id == self._reply_request_id:
            return False
        stopped_current = (request_id == self._playback_request_id
                           and self.session.playback_state in ('playing', 'paused'))
        obsolete = [pid for pid, uid in self._obsolete_playback_ids.items() if uid == request_id]
        if not (self._request_is_retired(request_id) or obsolete):
            return False
        self._quiescent_request_ids[request_id] = self.clock() + RETIRED_REQUEST_TTL_SECONDS
        while len(self._quiescent_request_ids) > MAX_RETIRED_REQUEST_IDS:
            self._quiescent_request_ids.popitem(last=False)
        for pid in obsolete:
            self._obsolete_playback_ids.pop(pid)
        if stopped_current:
            # A request-specific physical quiescence ACK is equivalent to a
            # lost STOPPED event for its own playback, never a global stop.
            self.session.on_playback_status(self.session.playback_id, 'stopped')
            self._playback_request_id = None
            if not self.input_has_aec:
                self._raw_playback_gate = False
        if not self.input_has_aec and (stopped_current or obsolete):
            self._raw_gate_until = max(self._raw_gate_until, self.clock() + 0.3)
            self._reset_audio()
        self._lifecycle('request_quiescent', request_id=request_id)
        return True

    def _finish_reply(self, reason, *, cancel=False):
        uid = self._reply_request_id
        if uid is None:
            return
        playback_ids = self._reply_playback_ids
        self._reply_request_id = None
        self._reply_receipt_deadline = self._reply_deadline = None
        self._reply_playback_ids = set()
        self._retired_request_ids[uid] = self.clock() + RETIRED_REQUEST_TTL_SECONDS
        self._retired_request_ids.move_to_end(uid)
        while len(self._retired_request_ids) > MAX_RETIRED_REQUEST_IDS:
            self._retired_request_ids.popitem(last=False)
        self._lifecycle('request_terminal', request_id=uid, reason=reason)
        if cancel:
            if self.cancel_request is not None:
                try:
                    self.cancel_request(uid)
                except Exception as error:
                    self.report('request_cancel_failed:' + type(error).__name__)
            for playback_id in playback_ids:
                self._stop_obsolete_speech(playback_id)
        # Keep web, chime and actual unrelated playback gates intact.
        self._reset_audio()
        if (cancel and not self.input_has_aec and self._raw_playback_gate
                and self.session.playback_id in playback_ids):
            self._lifecycle('request_recovery_blocked', request_id=uid,
                            reason='awaiting_playback_stop')
            self.report('request_recovery_blocked:awaiting_playback_stop')
        self.report('waiting_for_wake' if not self._input_blocked(self.clock())
                    else 'waiting_for_input_gate:' + self._input_gate_reason(self.clock()))

    def _stop_obsolete_speech(self, playback_id):
        if not isinstance(playback_id, str) or not playback_id.strip() or len(playback_id) > 200:
            return
        try:
            if self.stop_speech is not None:
                self.stop_speech(playback_id)
            else:
                self.publish_control(playback_id, 'stop')
        except Exception as error:
            self.report('obsolete_speech_stop_failed:' + type(error).__name__)

    def _expire_reply(self):
        if self._reply_request_id is None:
            return
        now = self.clock()
        if self._reply_deadline is not None and now >= self._reply_deadline:
            self._finish_reply('reply_timeout', cancel=True)
        elif self._reply_receipt_deadline is not None and now >= self._reply_receipt_deadline:
            self._finish_reply('receipt_timeout', cancel=True)

    def on_request_status(self, request_id, state, reason=''):
        """Correlate Agent receipt/terminal status without extending total wait."""
        if self.stopping.is_set():
            return
        self._expire_reply()
        if request_id != self._reply_request_id or self._reply_request_id is None:
            return
        if state == 'accepted':
            if self._reply_receipt_deadline is not None:
                self._reply_receipt_deadline = None
                self._lifecycle('request_accepted', request_id=request_id)
        elif state in ('rejected', 'failed', 'cancelled'):
            # Remote free-form reason is deliberately excluded from local diagnostics.
            self._finish_reply('agent_' + state, cancel=True)

    def start(self):
        """Open one microphone and start the bounded capture and inference workers."""
        self.phase = 'opening_microphone'
        self.recorder = self.recorder_factory()
        if self.recorder.sample_rate != 16000:
            raise ValueError('speech capture requires 16kHz PCM')
        self.phase = 'starting_microphone'
        self.recorder.start()
        self._started = True
        self._last_capture_at = monotonic()
        self.capture_thread = Thread(target=self._capture, name='stt-capture', daemon=True)
        self.asr_thread = Thread(target=self._infer, name='stt-asr', daemon=True)
        self.capture_thread.start()
        self.asr_thread.start()
        self.phase = 'running'
        self.report('waiting_for_wake')

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
            # A web call cannot complete an already accepted ordinary request.
            self._terminate('web_talk_started', preserve_reply=True)
            self._tail_stream = None
            self._drain(self.results)
        if quiet and not self._web_talk_quiet:
            # The guardian owns the speaker: drop current and queued speech for good.
            self._web_talk_quiet = True
            self._stop_speech('')
        return True

    def web_talk_is_active(self, lease_id):
        """Check the current lease at acknowledgement time, including renewed TTL."""
        self._expire_web_talk()
        return bool(not self.stopping.is_set() and lease_id is not None
                    and self._web_talk_lease_id == lease_id
                    and self.clock() < self._web_talk_deadline)

    def on_speech_request(self, playback_id, *, request_id=''):
        """Cancel Agent speech requested during a web talk before it is played."""
        if self.stopping.is_set():
            return
        self._expire_reply()
        if request_id and (self._request_is_retired(request_id)
                           or self._request_in_history(self._quiescent_request_ids, request_id)):
            self._stop_obsolete_speech(playback_id)
            return
        if request_id and request_id == self._reply_request_id:
            self.on_request_status(request_id, 'accepted')
            if len(self._reply_playback_ids) < 64:
                self._reply_playback_ids.add(playback_id)
        self._expire_web_talk()
        if (self._web_talk_quiet and isinstance(playback_id, str)
                and playback_id.strip() and len(playback_id) <= 200):
            self._stop_speech(playback_id)

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
        # Ordinary turns have no session ID, but must identify a real utterance.
        # Proactive sessions also use a blank UID for session-wide input failure.
        if self.publish_input_status is not None and (
                self.session.session_id or isinstance(uid, str) and uid.strip()):
            self.publish_input_status(
                self.session.session_id, uid, state)

    def _publish_transcript(self, utterance_id, text):
        lifecycle, self._utterance_lifecycle = self._utterance_lifecycle, None
        if not self.session.session_id:
            # One wake accepts one ordinary request, including during reply generation.
            self._terminate('waiting_for_reply')
            self._reply_request_id = utterance_id
            self._reply_receipt_deadline = self.clock() + self.receipt_timeout_s
            self._reply_deadline = self.clock() + self.reply_timeout_s
            self._reply_playback_ids = set()
            self._tail_stream = None
            self._lifecycle('request_published', request_id=utterance_id,
                            receipt_deadline=self._reply_receipt_deadline,
                            reply_deadline=self._reply_deadline)
        self._utterance_lifecycle = lifecycle
        try:
            self.publish_transcript(utterance_id, text)
        except Exception:
            self._finish_utterance_lifecycle('publish_failed')
            self._finish_reply('publish_failed', cancel=True)
            raise
        self._finish_utterance_lifecycle('published')

    def _capture(self):
        try:
            while not self.stopping.is_set():
                generation = self._audio_generation
                blocked = self._input_blocked(self.clock())
                busy = self._busy or self._pending is not None
                # The owner announces readiness only once a fresh read has
                # started after all cue/gate resets. A crossing read is still
                # discarded; no ADC timestamp is guessed to retain its tail.
                self._capture_open_generation = generation if not blocked else None
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

    def _infer(self):
        while not self.stopping.is_set():
            try:
                kind, generation, uid, pcm = self.jobs.get(timeout=0.05)
            except Empty:
                continue
            if self.stopping.is_set():
                break
            text, error_name = None, None
            retained_start_s = 0.0
            context = (self.diagnostics.context(
                kind=kind, generation=generation, session_id=self.session.session_id,
                utterance_id=uid[0] if isinstance(uid, tuple) else uid,
                revision=uid[1] if isinstance(uid, tuple) else None,
                audio_start_s=getattr(pcm, 'audio_start_s', 0.0),
                speech_end_s=getattr(pcm, 'speech_end_s', None),
                final=getattr(pcm, 'final', kind == 'command'),
            ) if self.diagnostics is not None else nullcontext())
            with context:
                error_detail = None
                try:
                    self._inference_started_at = monotonic()
                    # A reset can cancel a preview before the worker picks it up.
                    # Still return through results so endpoint ownership is released.
                    if generation != self._generation:
                        raise RuntimeError('stale inference job')
                    if isinstance(pcm, _StreamInput):
                        window = ({'audio_start_s': pcm.audio_start_s}
                                  if pcm.audio_start_s else {})
                        text = pcm.stream.transcribe(
                            pcm.pcm, 16000, final=pcm.final,
                            speech_end_s=pcm.speech_end_s, **window)
                        retained_start_s = getattr(pcm.stream, 'retained_start_s', 0.0)
                    else:
                        engine = self.wake if kind == 'wake' else self.transcriber
                        text = engine.transcribe(pcm, 16000)
                except Exception as error:
                    error_name = type(error).__name__
                    if self.diagnostics is not None:
                        error_detail = str(error)[:2048]
                finally:
                    self._inference_started_at = None
                    pcm = None
                    if self.diagnostics is not None:
                        self.diagnostics.event('inference_result', text=text,
                                               error=error_name, error_detail=error_detail)
            result = (kind, generation, uid, text, error_name, retained_start_s)
            while not self.stopping.is_set():
                try:
                    self.results.put(result, timeout=0.05)
                    break
                except Full:
                    continue

    @staticmethod
    def _drain(queue):
        while True:
            try:
                queue.get_nowait()
            except Empty:
                return

    def _reset_audio(self):
        self._audio_generation += 1
        self.command_stream.reset()
        self.wake_stream.reset()
        self._capture_id = None
        self._discard_capture = False
        self._tail_stream = None
        self._endpoint_candidate = None
        self._endpoint_result = None
        self._stream = None
        self._final_input = None
        # A finalized utterance may own busy while reusing its in-flight
        # endpoint decode. Cancelling that owner must not block future wakes.
        if self._endpoint_final is not None:
            self._busy = False
        self._endpoint_final = None
        self._drain(self.audio)

    def _terminate(self, reason, *, preserve_reply=False):
        self._finish_utterance_lifecycle(reason)
        if not preserve_reply:
            self._finish_reply(reason, cancel=True)
        if self.session.session_id and reason.startswith((
                'utterance_discarded:', 'transcription_queue_full',
                'audio_queue_overflow')):
            self._input_status('failed')
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
        self._drain(self.jobs)
        self._endpoint_job = None
        self._endpoint_requested_at = None
        self._endpoint_is_partial = False
        cancel = getattr(self.transcriber, 'cancel', None)
        if callable(cancel):
            cancel()
        self._pending = None
        self._retry_notice_deadline = None
        self._command_start_deadline = None
        self._utterance_playback_id = None
        self._wake_ready_pending = False
        self._reset_audio()
        self._tail_stream = tail
        self.report(reason)

    def poll(self):
        """Advance deadlines and consume bounded work without blocking ROS callbacks."""
        if self.stopping.is_set():
            return
        self._expire_web_talk()
        self._expire_reply()
        inference_started_at = self._inference_started_at
        if (inference_started_at is not None
                and monotonic() - inference_started_at >= self.inference_timeout_s):
            self._finish_utterance_lifecycle('inference_timeout')
            self.phase = 'transcribing'
            raise RuntimeError('inference deadline exceeded')
        if self.capture_error is None and self._capture_timed_out():
            self.capture_error = RuntimeError('microphone input timeout')
        if self.capture_error is not None:
            self._finish_utterance_lifecycle('microphone_failed:' + type(self.capture_error).__name__)
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
                self._capture_open_generation = generation
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
        self._report_input_gate(self.clock())

    def _expire_command_wait(self, captured_at):
        if (self._command_start_deadline is not None
                and captured_at >= self._command_start_deadline):
            self._fail_command('session_ended:input_timeout')
            return True
        return False

    def _input_blocked(self, captured_at):
        return self._input_gate_reason(captured_at) is not None

    def _input_gate_reason(self, captured_at):
        if self._web_talk_blocked(captured_at):
            return 'web_talk'
        if self._reply_request_id is not None:
            return 'reply_pending'
        if self._chime_playing or captured_at < self._chime_gate_until:
            return 'chime'
        if not self.input_has_aec and (
                self._raw_playback_gate or self._obsolete_playback_ids
                or captured_at < self._raw_gate_until):
            return 'raw_playback'
        return None

    def _report_input_gate(self, captured_at):
        reason = self._input_gate_reason(captured_at)
        if reason != self._last_gate_reason:
            self._last_gate_reason = reason
            self._lifecycle('input_gate', reason=reason or 'open')
        if (reason is None and self._wake_ready_pending and self.session.active
                and self.session.utterance_id is None
                and (not self._started or self._capture_open_generation == self._audio_generation)):
            self._wake_ready_pending = False
            self._command_start_deadline = self.clock() + self.command_stream.settings.start_timeout_s
            self.report('wake_input_ready')
            self._lifecycle('input_ready', source='wake')

    def _publish_control(self, playback_id, command):
        if command == 'pause' and (
            not self.input_has_aec
            or self._pending is not None and self._pending[1] != playback_id
        ):
            return
        self.publish_control(playback_id, command)

    def feed(self, pcm, *, captured_at=None, busy_at_capture=False):
        """Consume captured PCM on the owner thread; the microphone remains open."""
        self._expire_web_talk()
        if captured_at is None and not self._input_blocked(self.clock()):
            # Explicit owner-thread PCM injection is itself evidence of a
            # current, eligible capture boundary (the hardware path tags time).
            self._capture_open_generation = self._audio_generation
        captured_at = self.clock() if captured_at is None else captured_at
        self._report_input_gate(captured_at)
        if self.stopping.is_set() or self._input_blocked(captured_at):
            return
        self._event_time = captured_at
        try:
            self._feed(pcm, busy_at_capture)
        finally:
            self._event_time = None

    def _feed(self, pcm, busy_at_capture):
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
        for event in stream.feed(pcm, start_blocked=start_blocked):
            if event.status == 'speech_started':
                self._discard_capture = (event.start_blocked or self._busy
                                         or self._pending is not None)
                if self._discard_capture:
                    self.report('speech_discarded:busy')
                    self._lifecycle('utterance_terminal', utterance_id=str(uuid4()),
                                    session_id=self.session.session_id,
                                    reason='busy', source='discarded_candidate')
                    continue
                if self.session.active:
                    self._command_start_deadline = None
                    self._capture_id = self.session.user_speech_started()
                    self._start_utterance_lifecycle(self._capture_id)
                    self._input_status('started', self._capture_id)
                    self._endpoint_result = None
                    self._stream = (self._stream_factory() if callable(self._stream_factory)
                                    and self.command_stream.partial_interval_s is not None
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
                audio_start_s=event.audio_start_s)
            self.jobs.put_nowait((kind, key[0], (uid, event.revision),
                                 payload))
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

    def _inference_input(self, pcm, *, final=False, silence_s=0.0, audio_start_s=0.0):
        if self._stream is None:
            return pcm
        speech_end_s = audio_start_s + max(0.0, len(pcm) / 32000 - silence_s)
        return _StreamInput(pcm, self._stream, final, speech_end_s, audio_start_s)

    def _complete_capture(self, kind, event):
        uid = self._capture_id
        key = (self._generation, uid, event.revision)
        self._capture_id = None
        self._endpoint_candidate = None
        self._busy = True
        self.report('transcribing' if kind == 'command' else 'recognizing_wake')
        if kind == 'command':
            self.report(f'endpoint_finalized:silence_s={event.silence_s:.2f}')
            self._play_endpoint_chime()
            if (self._endpoint_result is not None and self._endpoint_result[0] == key
                    and self._usable_text(*self._endpoint_result[1:])):
                _, text, error = self._endpoint_result
                self._endpoint_result = None
                self._busy = False
                self._accept_result('command', key[0], uid, text, error)
                return
            if self._endpoint_job == key and not self._endpoint_is_partial:
                self._endpoint_final = key
                self._final_input = self._inference_input(
                    event.pcm, final=True, silence_s=event.silence_s,
                    audio_start_s=event.audio_start_s)
                return
        # A resumed utterance can finish before its obsolete candidate is taken
        # by the worker. Replace only that queued candidate with the final audio.
        if self.jobs.full():
            try:
                stale = self.jobs.get_nowait()
            except Empty:
                pass
            else:
                if stale[0] not in ('endpoint', 'partial'):
                    self._terminate('transcription_queue_full')
                    return
                if self._endpoint_job == (stale[1], *stale[2]):
                    self._endpoint_job = None
        payload = (self._inference_input(event.pcm, final=True, silence_s=event.silence_s,
                                         audio_start_s=event.audio_start_s)
                   if kind == 'command' else event.pcm)
        self.jobs.put_nowait((kind, self._generation, uid, payload))

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
            self._chime_gate_until = self.clock() + 0.3
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
            self._chime_gate_until = self.clock() + 0.3
            self._audio_generation += 1
            # Keep the completed PCM, stream, generation and endpoint job alive.
            # Only unfinished capture fragments and speaker echo are discarded.
            self.command_stream.reset()
            self._drain(self.audio)
            self._chime_playing = False

    def _accept_endpoint(self, generation, token, text, error_name, retained_start_s=0.0,
                         *, partial=False):
        uid, revision = token
        key = (generation, uid, revision)
        if self._endpoint_job == key:
            self._endpoint_job = None
            elapsed = max(0.0, self.clock() - self._endpoint_requested_at)
            self._endpoint_requested_at = None
            self.report(f'endpoint_checked:wait_s={elapsed:.3f}')
        if generation != self._generation:
            self._lifecycle('inference_discarded', utterance_id=uid,
                            reason='stale_generation', inference_generation=generation)
            return
        if self._endpoint_final == key:
            self._endpoint_final = None
            payload, self._final_input = self._final_input, None
            if self._usable_text(text, error_name):
                self._busy = False
                self._accept_result('command', generation, uid, text, error_name)
            else:
                self.report('partial_failed')
                self.jobs.put_nowait(('command', generation, uid, payload))
            return
        if uid != self._capture_id or not self.session.active:
            return
        if self._usable_text(text, error_name):
            # Apply only to this live utterance. Its VAD revision may advance
            # during inference; the acknowledged prefix still remains valid.
            self.command_stream.collector.discard_before(retained_start_s)
            self.report('partial_ready')
            if self.on_partial is not None:
                self.on_partial(uid, text)
        else:
            self.report('partial_failed')
        # Speech-time previews do not cover quiet/weak trailing syllables yet.
        # Only an endpoint snapshot can supply a final result, even if VAD's
        # last voiced revision did not change while that tail was recorded.
        if partial or revision != self.command_stream.collector.revision:
            return
        self._endpoint_result = (key, text, error_name)
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
            return
        if (self.command_stream.collector.silent_frames * 0.02 >= 1.0
                and error_name is None and is_complete_korean_utterance(text)):
            event = self.command_stream.finish_endpoint(revision)
            if event is not None:
                self._complete_capture('command', event)

    def _accept_result(self, kind, generation, uid, text, error_name, retained_start_s=0.0):
        if generation != self._generation:
            self._lifecycle('inference_discarded', utterance_id=uid,
                            reason='stale_generation', inference_generation=generation)
            return
        if kind != 'wake' and (not self.session.active or uid != self.session.utterance_id):
            self._lifecycle('inference_discarded', utterance_id=uid,
                            reason='stale_utterance', inference_generation=generation)
            return
        failure = (
            'transcription_failed:' + error_name if error_name is not None
            else 'empty_transcript' if not isinstance(text, str) or not text.strip()
            else None
        )
        if failure is not None:
            if kind == 'wake':
                self._terminate(failure)
            elif not self.session.session_id:
                self._fail_command(failure)
            else:
                # Agent-owned confirmations retain their correlated failure policy.
                self._finish_utterance_lifecycle(failure)
                self._input_status('failed', uid)
                self.session.discard_utterance(uid)
                self._utterance_playback_id = None
                self.report(failure)
            return
        if kind == 'wake':
            command = split_wake_command(text)
            if command is not None:
                self.session.activate()
                self._reset_audio()
                self.report('wake_detected')
                if command:
                    # The wake decode already contains the complete captured command.
                    # Preserve its suffix exactly and do not decode that PCM again.
                    uid = self.session.user_speech_started()
                    self._start_utterance_lifecycle(uid, source='wake_command')
                    self._input_status('started', uid)
                    self._utterance_playback_id = self.session.interrupted_playback_id
                    self._play_endpoint_chime()
                    self._accept_result('command', self._generation, uid, command, None)
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
                        self._chime_gate_until = self.clock() + (0.0 if self.input_has_aec else 0.3)
                        self._reset_audio()
                        self._chime_playing = False
                if self.session.active:
                    self._command_start_deadline = None
                    self._wake_ready_pending = True
                    self._report_input_gate(self.clock())
            else:
                self.report('not_wake')
            return
        pid = self._utterance_playback_id
        if pid is not None:
            if pid != self.session.playback_id:
                self._terminate('addressee_unknown:stale_playback')
                return
            self._pending = (uid, pid, text, self.clock() + 45.0)
            self.report('awaiting_addressee:timeout_s=45')
            self.publish_interruption(uid, pid, text)
        else:
            self.session.finish_utterance(uid, text, addressed=True)
        self._utterance_playback_id = None

    def on_playback_status(self, playback_id, state, *, interim=False, request_id=''):
        """Track acknowledged playback and discard raw echo on gate transitions."""
        if self.stopping.is_set():
            return
        self.poll()
        if request_id and self._request_in_history(self._quiescent_request_ids, request_id):
            # TTS has already installed a tombstone and acknowledged physical
            # silence. Delayed topic events cannot reintroduce this old sound.
            if state in ('playing', 'paused'):
                self._stop_obsolete_speech(playback_id)
            return
        if request_id and (self._request_is_retired(request_id)
                           or self._obsolete_playback_ids.get(playback_id) == request_id):
            if state in ('playing', 'paused'):
                if (not self.input_has_aec and isinstance(playback_id, str)
                        and playback_id.strip() and len(playback_id) <= 200
                        and playback_id not in self._obsolete_playback_ids):
                    # A cancellation can race a previously unseen PLAYING event.
                    # Track its actual sound separately, without attaching an old
                    # request to the new conversation or unlocking a new reply.
                    if len(self._obsolete_playback_ids) >= MAX_RETIRED_REQUEST_IDS:
                        raise RuntimeError('obsolete playback tracking overflow')
                    self._obsolete_playback_ids[playback_id] = request_id
                    if self.session.utterance_id is not None:
                        self._terminate('utterance_discarded:obsolete_playback_without_aec',
                                        preserve_reply=True)
                    else:
                        self._reset_audio()
                self._stop_obsolete_speech(playback_id)
            elif state in ('finished', 'failed', 'stopped'):
                stopped_obsolete = self._obsolete_playback_ids.get(playback_id) == request_id
                if stopped_obsolete:
                    self._obsolete_playback_ids.pop(playback_id)
                stopped_current = (
                    playback_id == self.session.playback_id
                    and self.session.playback_state in ('playing', 'paused'))
                if stopped_current:
                    # Only actual cessation of this exact old playback can
                    # release its gate; never overwrite a newer playback.
                    self.session.on_playback_status(playback_id, state, interim=interim)
                    self._playback_request_id = None
                    if not self.input_has_aec:
                        self._raw_playback_gate = False
                if not self.input_has_aec and (stopped_obsolete or stopped_current):
                    self._raw_gate_until = self.clock() + 0.3
                    self._reset_audio()
            return
        if request_id and request_id == self._reply_request_id:
            if state in ('playing', 'paused'):
                self.on_request_status(request_id, 'accepted')
                if len(self._reply_playback_ids) < 64:
                    self._reply_playback_ids.add(playback_id)
            elif state in ('finished', 'failed', 'stopped'):
                self._reply_playback_ids.discard(playback_id)
        if (self._web_talk_quiet and state in ('playing', 'paused')
                and isinstance(playback_id, str) and playback_id.strip()):
            # A request that raced the web talk start is stopped as soon as it sounds.
            self._stop_speech(playback_id)
        previous = (self.session.playback_id, self.session.playback_state)
        self.session.on_playback_status(playback_id, state, interim=interim)
        if self._wake_ready_pending or self._command_start_deadline is not None:
            # Wake listening owns a five-second window from actual readiness;
            # unrelated playback must not create an earlier competing deadline.
            self.session.deadline = None
        current = (self.session.playback_id, self.session.playback_state)
        if previous != current and current[0] == playback_id:
            self._playback_request_id = request_id if current[1] in ('playing', 'paused') else None
        if previous != current and not self.input_has_aec:
            if current[1] in ('playing', 'paused') and self.session.utterance_id is not None:
                self._terminate('utterance_discarded:playback_without_aec')
            self._raw_playback_gate = current[1] == 'playing'
            # This drain guard limits queued playback tails; it is not AEC.
            self._raw_gate_until = self.clock() + 0.3
            self._reset_audio()
            if self._raw_playback_gate:
                self.report('barge_in_requires_aec')
        if self.session.interrupted_playback_id is not None:
            self._utterance_playback_id = self.session.interrupted_playback_id
        if self._pending is not None and self._pending[1] != self.session.playback_id:
            self._terminate('addressee_unknown:stale_playback')
        if (self._reply_request_id is not None and request_id == self._reply_request_id
                and not interim and state in ('finished', 'failed', 'stopped')):
            # Failed synthesis can terminate before PLAYING; progress cannot unlock input.
            self._finish_reply('playback_' + state)

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
            if decision == 'not_addressed':
                self._finish_utterance_lifecycle('not_addressed')
            self.session.finish_utterance(uid, text, addressed=decision == 'addressed')
        else:
            self.report('invalid_addressee_decision')

    def prepare_shutdown(self):
        """Publish bounded request cancellation before potentially blocking cleanup."""
        if self._shutdown_prepared:
            return
        self._shutdown_prepared = True
        self._finish_utterance_lifecycle('shutdown')
        self._finish_reply('shutdown', cancel=True)
        self.stopping.set()

    def close(self):
        """Stop capture before deletion and suppress any late local inference result."""
        if self._closed:
            return
        self._closed = True
        self.prepare_shutdown()
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
        self._drain(self.jobs)
        self._drain(self.results)
        self._endpoint_job = None
        self._endpoint_requested_at = None
        self._endpoint_candidate = None
        self._endpoint_result = None
        self._endpoint_final = None
        self._final_input = None
        self._stream = None
        if failure is not None:
            raise failure
