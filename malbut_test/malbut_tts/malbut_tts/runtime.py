"""Serialize speech requests and keep playback controls scoped to one ID."""

from dataclasses import dataclass, field
import heapq
import logging
from threading import Condition, Event, Thread
from typing import Callable, Optional
from uuid import uuid4


DIALOGUE = 0
NOTIFICATION = 1
TERMINAL_STATES = frozenset(('finished', 'failed', 'stopped'))


@dataclass
class _Request:
    playback_id: str
    text: str
    validate: Optional[Callable] = None
    cancel: Event = field(default_factory=Event)
    player: object = None
    state: str = 'generating'
    command: Optional[str] = None
    control_error: Optional[Exception] = None


class SpeechRuntime:
    """Run one synthesis/playback job, choosing pending jobs by priority/FIFO.

    The synthesizer yields ``(audio, sample_rate)`` pairs. A player consumes
    them while synthesis continues and finishes only after device drain.
    Status callbacks should be short (for example, put into a ROS queue).
    """

    def __init__(self, synthesizer, player_factory, on_status, logger=None):
        self._synthesizer = synthesizer
        self._player_factory = player_factory
        self._on_status = on_status
        self._logger = logger or logging.getLogger(__name__)
        self._condition = Condition()
        self._pending = []
        self._sequence = 0
        self._active = None
        self._closed = False
        self._worker = Thread(target=self._run, name='tts', daemon=True)
        self._worker.start()

    def submit(self, text, request_type=DIALOGUE, *, validate=None):
        """Queue the original text and return its ID, or ignore invalid input."""
        if not isinstance(text, str) or not text.strip():
            self._logger.warning('tts_text_ignored: blank response')
            return None
        if type(request_type) is not int or request_type not in (0, 1):
            self._logger.warning('tts_request_ignored: invalid request_type')
            return None
        with self._condition:
            if self._closed:
                return None
            request = _Request(str(uuid4()), text, validate)
            heapq.heappush(self._pending, (
                request_type, self._sequence, request,
            ))
            self._sequence += 1
            self._condition.notify_all()
            return request.playback_id

    def control(self, playback_id, command):
        """Accept a valid control for the active request without replacing it."""
        with self._condition:
            request = self._active
            if (request is None or request.playback_id != playback_id
                    or request.state in TERMINAL_STATES
                    or request.cancel.is_set()):
                return False
            player = request.player
            if command == 'stop':
                request.cancel.set()
                request.command = command
            elif (command == 'pause' and request.state == 'playing'
                  and request.command is None and player is not None):
                request.command = command
            elif (command == 'resume' and request.state == 'paused'
                  and request.command is None and player is not None):
                request.command = command
            else:
                return False
        # Device control may wait for its owner thread; do not hold the
        # runtime condition while that thread reports a playback state.
        if command == 'stop':
            if player is not None:
                try:
                    player.stop()
                except Exception as error:
                    with self._condition:
                        request.control_error = error
                    self._logger.error(f'tts_control_failed: {error}')
            return True
        accepted = getattr(player, command)()
        if not accepted:
            with self._condition:
                if request.command == command:
                    request.command = None
        return accepted

    def close(self):
        """Cancel active work, drop waiting jobs, and close the worker."""
        with self._condition:
            self._closed = True
            self._pending.clear()
            request = self._active
            if request is not None and request.state in TERMINAL_STATES:
                request = None
            if request is not None:
                request.cancel.set()
            self._condition.notify_all()
        try:
            if request is not None and request.player is not None:
                request.player.stop()
        finally:
            self._worker.join(timeout=10)
        if self._worker.is_alive():
            raise TimeoutError('TTS synthesis did not stop within 10 seconds')

    def _status(self, request, state):
        with self._condition:
            if (self._active is not request
                    or request.state in TERMINAL_STATES
                    or request.state == state):
                return
            if request.cancel.is_set() and state not in TERMINAL_STATES:
                return
            request.state = state
            if ((request.command == 'pause' and state == 'paused')
                    or (request.command == 'resume' and state == 'playing')):
                request.command = None
            try:
                self._on_status(request.playback_id, state)
            except Exception as error:
                self._logger.error(f'tts_status_failed: {error}')

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: self._closed or bool(self._pending),
                )
                if self._closed:
                    return
                _, _, request = heapq.heappop(self._pending)
                self._active = request
            self._play(request)
            with self._condition:
                self._active = None

    def _play(self, request):
        player = None
        chunks = None
        state = 'finished'
        cleanup_failed = False
        try:
            player = self._player_factory(
                on_state=lambda state: self._status(request, state),
                cancel_event=request.cancel,
            )
            with self._condition:
                request.player = player
            if not request.cancel.is_set():
                chunks = iter(self._synthesizer.generate(
                    request.text, request.cancel,
                ))
                has_audio = False
                sentence_mode = getattr(self._synthesizer, 'sentence_streaming', False)
                while not request.cancel.is_set():
                    if sentence_mode:
                        # Do not start a third sentence when two are queued,
                        # even if synthesis is faster than playback or paused.
                        player.wait_for_capacity(max_pending=2)
                    try:
                        audio, sample_rate = next(chunks)
                    except StopIteration:
                        break
                    if request.cancel.is_set():
                        break
                    if not has_audio and request.validate is not None:
                        request.validate()
                    player.write(audio, sample_rate)
                    has_audio = True
                if not request.cancel.is_set():
                    if not has_audio:
                        raise RuntimeError('TTS generated no audio')
                    player.finish()
        except Exception as error:
            state = 'failed'
            if not request.cancel.is_set():
                self._logger.error(
                    f'tts_playback_failed: {request.playback_id}: {error}',
                )
        finally:
            # No new request starts until both iterator and device are closed.
            for resource in (chunks, player):
                if resource is not None:
                    try:
                        close = getattr(resource, 'close', None)
                        if close is not None:
                            close()
                    except Exception as error:
                        cleanup_failed = True
                        state = 'failed'
                        self._logger.error(f'tts_cleanup_failed: {error}')
            # A stop racing the final drain wins over normal completion.
            with self._condition:
                if cleanup_failed or request.control_error is not None:
                    state = 'failed'
                elif request.cancel.is_set():
                    state = 'stopped'
                self._status(request, state)
