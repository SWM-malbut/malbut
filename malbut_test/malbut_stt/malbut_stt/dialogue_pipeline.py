"""Keep continuous capture and local inference outside serialized dialogue events."""

import math
from dataclasses import dataclass
from queue import Empty, Full, Queue
from threading import Event, Thread
from time import monotonic

from malbut_stt.audio import CaptureSettings
from malbut_stt.conversation import ConversationSession
from malbut_stt.endpoint import is_complete_korean_utterance
from malbut_stt.pipeline import pcm_bytes
from malbut_stt.streaming import StreamingUtteranceCollector
from malbut_stt.wake import is_wake_phrase


@dataclass(frozen=True)
class _StreamInput:
    pcm: bytes
    stream: object
    final: bool
    speech_end_s: float


class DialoguePipeline:
    """Run one capture thread and one ASR worker; poll and callbacks share one owner.

    One utterance may await inference or addressee classification. Additional
    utterances are explicitly discarded through their endpoint while it is busy.
    ``input_has_aec`` asserts that the selected microphone already supplies AEC;
    this class does not remove playback echo from raw microphone audio.
    """

    def __init__(self, *, recorder_factory, wake, transcriber, is_speech,
                 publish_transcript, publish_control, publish_interruption,
                 report, settings=None, input_has_aec=False, clock=monotonic,
                 on_wake=None, endpoint_predecode_s: float | None = None,
                 partial_interval_s: float | None = 2.0, on_partial=None):
        self.recorder_factory = recorder_factory
        self.wake = wake
        self.transcriber = transcriber
        self.publish_interruption = publish_interruption
        self.publish_control = publish_control
        self.report = report
        self.clock = clock
        self._event_time = None
        self.on_wake = on_wake
        self.on_partial = on_partial
        self._stream_factory = getattr(transcriber, 'create_stream', None)
        self._stream = None
        self.input_has_aec = input_has_aec
        self.session = ConversationSession(
            clock=lambda: self.clock() if self._event_time is None else self._event_time,
            publish_transcript=publish_transcript,
            publish_control=self._publish_control,
        )
        settings = settings or CaptureSettings(silence_timeout_s=2.0)
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
        self.recorder = None
        self.capture_thread = None
        self.asr_thread = None
        self.capture_error = None
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
        self._raw_playback_gate = False
        self._raw_gate_until = 0.0
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

    def start(self):
        """Open one microphone and start the bounded capture and inference workers."""
        self.phase = 'opening_microphone'
        self.recorder = self.recorder_factory()
        if self.recorder.sample_rate != 16000:
            raise ValueError('speech capture requires 16kHz PCM')
        self.phase = 'starting_microphone'
        self.recorder.start()
        self._started = True
        self.phase = 'reading_microphone'
        samples = self.recorder.read()
        if len(samples) != 512:
            raise ValueError('speech microphone returned an incomplete frame')
        # Prove capture and VAD before the node publishes readiness. Discard this
        # startup frame so validation cannot become a wake or user utterance.
        self.wake_stream.is_speech(pcm_bytes(samples)[:640], 16000)
        self.capture_thread = Thread(target=self._capture, name='stt-capture', daemon=True)
        self.asr_thread = Thread(target=self._infer, name='stt-asr', daemon=True)
        self.capture_thread.start()
        self.asr_thread.start()
        self.phase = 'running'
        self.report('waiting_for_wake')

    def _capture(self):
        try:
            while not self.stopping.is_set():
                generation = self._audio_generation
                blocked = self._input_blocked(self.clock())
                busy = self._busy or self._pending is not None
                samples = self.recorder.read()
                captured_at = self.clock()
                if self.stopping.is_set():
                    break
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
            if not self.stopping.is_set():
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
            try:
                if isinstance(pcm, _StreamInput):
                    text = pcm.stream.transcribe(
                        pcm.pcm, 16000, final=pcm.final, speech_end_s=pcm.speech_end_s)
                else:
                    engine = self.wake if kind == 'wake' else self.transcriber
                    text = engine.transcribe(pcm, 16000)
            except Exception as error:
                error_name = type(error).__name__
            finally:
                pcm = None
            result = (kind, generation, uid, text, error_name)
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

    def _terminate(self, reason):
        stream = self.command_stream if self.session.active else self.wake_stream
        tail = self._tail_stream
        if tail is None and self._discard_capture and stream.collector.started:
            tail = StreamingUtteranceCollector(stream.is_speech, settings=stream.settings)
            tail.discarding = True
            tail.quiet_frames = stream.collector.silent_frames
        self.session.terminate()
        self._generation += 1
        self._pending = None
        self._utterance_playback_id = None
        self._reset_audio()
        self._tail_stream = tail
        self.report(reason)

    def poll(self):
        """Advance deadlines and consume bounded work without blocking ROS callbacks."""
        if self.stopping.is_set():
            return
        if self.capture_error is not None:
            self.phase = 'reading_microphone'
            raise self.capture_error
        if self._pending is not None and self.clock() >= self._pending[3]:
            self._terminate('addressee_unknown:timeout')
        if self.overflow.is_set():
            self._terminate('audio_queue_overflow')
            self.overflow.clear()
        for _ in range(self.audio.maxsize):
            try:
                generation, captured_at, pcm, busy = self.audio.get_nowait()
            except Empty:
                break
            if generation == self._audio_generation:
                self.feed(pcm, captured_at=captured_at, busy_at_capture=busy)
        try:
            result = self.results.get_nowait()
        except Empty:
            pass
        else:
            if result[0] in ('endpoint', 'partial'):
                self._accept_endpoint(*result[1:], partial=result[0] == 'partial')
            else:
                self._busy = False
                self._accept_result(*result)
        self._submit_endpoint()
        self._finish_ready_endpoint()
        if self.session.tick():
            self._terminate('session_ended:tts_timeout')

    def _input_blocked(self, captured_at):
        return not self.input_has_aec and (
            self._raw_playback_gate or captured_at < self._raw_gate_until
        )

    def _publish_control(self, playback_id, command):
        if command == 'pause' and (
            not self.input_has_aec
            or self._pending is not None and self._pending[1] != playback_id
        ):
            return
        self.publish_control(playback_id, command)

    def feed(self, pcm, *, captured_at=None, busy_at_capture=False):
        """Consume captured PCM on the owner thread; the microphone remains open."""
        captured_at = self.clock() if captured_at is None else captured_at
        if self.stopping.is_set() or self._input_blocked(captured_at):
            return
        self._event_time = captured_at
        try:
            self._feed(pcm, busy_at_capture)
        finally:
            self._event_time = None

    def _feed(self, pcm, busy_at_capture):
        if self.session.tick():
            self._terminate('session_ended:tts_timeout')
        if self._tail_stream is not None:
            self._tail_stream.feed(pcm)
            if not self._tail_stream.discarding:
                self._tail_stream = None
            return
        stream = self.command_stream if self.session.active else self.wake_stream
        for event in stream.feed(pcm):
            if event.status == 'speech_started':
                self._discard_capture = busy_at_capture or self._busy or self._pending is not None
                if self._discard_capture:
                    self.report('speech_discarded:busy')
                    continue
                if self.session.active:
                    self._capture_id = self.session.user_speech_started()
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
            elif event.status in ('complete', 'too_long'):
                if self._discard_capture:
                    self._discard_capture = False
                    continue
                if event.status == 'too_long':
                    if self.session.active:
                        self._terminate('utterance_discarded:too_long')
                        stream.discarding = True
                        self._tail_stream = stream
                    else:
                        self.report('wake_too_long')
                    continue
                kind = 'command' if self.session.active else 'wake'
                self._complete_capture(kind, event)

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
                event.pcm if self._stream is not None else pcm, silence_s=event.silence_s)
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

    def _inference_input(self, pcm, *, final=False, silence_s=0.0):
        if self._stream is None:
            return pcm
        speech_end_s = max(0.0, len(pcm) / 32000 - silence_s)
        return _StreamInput(pcm, self._stream, final, speech_end_s)

    def _complete_capture(self, kind, event):
        uid = self._capture_id
        key = (self._generation, uid, event.revision)
        self._capture_id = None
        self._endpoint_candidate = None
        self._busy = True
        self.report('transcribing' if kind == 'command' else 'recognizing_wake')
        if kind == 'command':
            self.report(f'endpoint_finalized:silence_s={event.silence_s:.2f}')
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
                    event.pcm, final=True, silence_s=event.silence_s)
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
        payload = (self._inference_input(event.pcm, final=True, silence_s=event.silence_s)
                   if kind == 'command' else event.pcm)
        self.jobs.put_nowait((kind, self._generation, uid, payload))

    def _accept_endpoint(self, generation, token, text, error_name, *, partial=False):
        uid, revision = token
        key = (generation, uid, revision)
        if self._endpoint_job == key:
            self._endpoint_job = None
            elapsed = max(0.0, self.clock() - self._endpoint_requested_at)
            self._endpoint_requested_at = None
            self.report(f'endpoint_checked:wait_s={elapsed:.3f}')
        if generation != self._generation:
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

    def _accept_result(self, kind, generation, uid, text, error_name):
        if generation != self._generation:
            return
        if kind != 'wake' and (not self.session.active or uid != self.session.utterance_id):
            return
        failure = (
            'transcription_failed:' + error_name if error_name is not None
            else 'empty_transcript' if not isinstance(text, str) or not text.strip()
            else None
        )
        if failure is not None:
            if kind == 'wake':
                self._terminate(failure)
            else:
                self.session.discard_utterance(uid)
                self._utterance_playback_id = None
                self.report(failure)
            return
        if kind == 'wake':
            if is_wake_phrase(text):
                self.session.activate()
                self._reset_audio()
                self.report('wake_detected')
                if self.on_wake is None:
                    self.report('wake_chime_unavailable')
                else:
                    self.on_wake()
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

    def on_playback_status(self, playback_id, state):
        """Track acknowledged playback and discard raw echo on gate transitions."""
        if self.stopping.is_set():
            return
        self.poll()
        previous = (self.session.playback_id, self.session.playback_state)
        self.session.on_playback_status(playback_id, state)
        current = (self.session.playback_id, self.session.playback_state)
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
        self._closed = True
        self.stopping.set()
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
