"""Linux PulseAudio microphone/render clock alignment and startup calibration.

Runs only inside EchoCancelledRecorder's disposable process. Device defaults,
Pulse modules, system services and persistent mixer settings are never changed.
"""

from collections import deque
import hashlib
import json
import math
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
from threading import Thread, Event
from time import monotonic, thread_time
import wave


def track_capture_clock(anchors, observed_difference):
    """Follow slow device clock drift; reject discontinuities instead of guessing."""
    drift = observed_difference - (anchors['mic'] - anchors['ref'])
    if not math.isfinite(drift) or abs(drift) > 0.04:
        raise RuntimeError('AEC reference clock changed')
    # At most 1 ms per second, within the linear filter's causal tail.
    anchors['mic'] += max(-0.001, min(0.001, drift))


class StartupPriming:
    """Keep initialization audio private until ordered processing catches up.

    A half-deadline margin leaves 200 ms for the parent's capture thread/IPC.
    Once ready, this gate never drops audio: the recorder's existing 400 ms
    guard remains responsible for every live frame.
    """

    MAX_AGE_S = 0.2
    TIMEOUT_S = 5.0

    def __init__(self, live_after):
        self.live_after = live_after
        self.deadline = live_after + self.TIMEOUT_S
        self.ready = False
        self.historical_frames = 0
        self.backlog_frames = 0

    def check_deadline(self, now):
        if not self.ready and now >= self.deadline:
            raise TimeoutError('AEC startup catch-up timed out')

    def accept(self, timestamp, now):
        if self.ready:
            return True
        self.check_deadline(now)
        if not math.isfinite(timestamp) or timestamp > now:
            raise RuntimeError('Invalid AEC startup capture timestamp')
        if timestamp < self.live_after:
            self.historical_frames += 1
            return False
        if now - timestamp > self.MAX_AGE_S:
            self.backlog_frames += 1
            return False
        self.ready = True
        return True


def run(config, output, stop):
    from .scheduling import configure_audio_worker_priority
    configure_audio_worker_priority()
    if 'raw_source' in config:
        run_raw(config['raw_source'], output, stop)
        return
    warm = None
    threads = []
    local_stop = Event()
    from .timing import TimingTrace, queue_depth
    trace = TimingTrace('worker_aec')
    latest_timing = {}
    previous_put_s = None
    priming = None
    try:
        os.environ['OPENBLAS_NUM_THREADS'] = '1'
        os.environ['OMP_NUM_THREADS'] = '1'
        import numpy as np
        import sounddevice as sd
        from .processor import EchoProcessor, calibrate
        from .pulse import Capture

        source, sink = config['source'], config['sink']
        def listing(kind):
            return json.loads(subprocess.check_output(
                ['pactl', '--format=json', 'list', kind], text=True, timeout=3))
        if source not in {s['name'] for s in listing('sources')}:
            raise RuntimeError('Configured AEC microphone is unavailable')
        sinks = {s['name']: s['index'] for s in listing('sinks')}
        if sink not in sinks:
            raise RuntimeError('Configured AEC speaker is unavailable')
        if any(stream['sink'] == sinks[sink] for stream in listing('sink-inputs')):
            raise RuntimeError('Speaker is busy; calibration deferred to next launch')
        models = Path(config['models'])
        manifest = json.loads((Path(__file__).parent / 'models.json').read_text())
        for name, digest in manifest['sha256'].items():
            if hashlib.sha256((models / name).read_bytes()).hexdigest() != digest:
                raise RuntimeError('AEC model checksum mismatch')
        with wave.open(config['calibration_audio']) as wav:
            if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (16000, 1, 2):
                raise ValueError('Calibration audio must be mono 16 kHz PCM16')
            if not 2 <= wav.getnframes() / 16000 <= 8:
                raise ValueError('Calibration audio must last 2 to 8 seconds')
            calibration_audio = np.frombuffer(wav.readframes(wav.getnframes()), '<i2').astype(np.float32) / 32768
        os.environ['PULSE_SINK'] = sink
        # Keep the sink clock running before either capture starts. Suspending
        # the monitor produces a large, variable startup timestamp offset.
        warm = sd.OutputStream(device='pulse', samplerate=16000, channels=1,
                               blocksize=512, latency=0.064,
                               callback=lambda out, *_: out.fill(0))
        warm.start()
        events = Queue(maxsize=2048)
        def capture(name, device):
            cap = None
            try:
                cap = Capture(device, hop=512, trace_timing=trace.enabled)
                index = 0
                while not stop.is_set() and not local_stop.is_set():
                    x, timestamp, _ = cap.read()
                    event = (name, index, x, timestamp)
                    if trace.enabled:
                        timing = dict(cap.last_timing or {})
                        timing['capture_enqueue_s'] = monotonic()
                        event += (timing,)
                    events.put(event, timeout=0.2)
                    index += 1
            except Exception as error:
                try:
                    events.put(('error', str(error), None, None), timeout=0.1)
                finally:
                    local_stop.set()
            finally:
                if cap is not None:
                    cap.close()
        threads = [Thread(target=capture, args=(n, d), daemon=True)
                   for n, d in [('mic', source), ('ref', sink + '.monitor')]]
        for thread in threads:
            thread.start()
        offsets = {n: deque(maxlen=32) for n in ('mic', 'ref')}
        recorded = {n: [] for n in ('mic', 'ref')}
        recorded_start = 0
        latest_index = {n: -1 for n in ('mic', 'ref')}
        calibration_attempt = 1
        reference = {}
        reference_timing = {}
        origin = monotonic()
        playback_started = None
        playback_error = []
        playback_done = Event()
        processor = anchors = calibration = None
        def play_calibration():
            try:
                with sd.OutputStream(device='pulse', samplerate=16000,
                                     channels=1, latency=0.064) as player:
                    for begin in range(0, len(calibration_audio), 320):
                        if stop.is_set() or local_stop.is_set():
                            return
                        player.write(calibration_audio[begin:begin + 320])
            except Exception as error:
                playback_error.append(str(error))
            finally:
                playback_done.set()
        while not stop.is_set():
            if priming is not None and not priming.ready:
                priming.check_deadline(monotonic())
            if local_stop.is_set():
                raise RuntimeError('AEC capture worker failed')
            if playback_error:
                raise RuntimeError('Calibration playback failed')
            try:
                event = events.get(timeout=0.5)
                name, index, x, timestamp = event[:4]
                timing = dict(event[4]) if len(event) == 5 else None
                if timing is not None:
                    timing['worker_dequeued_s'] = monotonic()
                    timing['worker_event_wait_s'] = (
                        timing['worker_dequeued_s'] - timing['capture_enqueue_s'])
                    timing['event_queue_depth'] = queue_depth(events)
            except Empty:
                raise RuntimeError('AEC microphone/reference timed out')
            if name == 'error':
                raise RuntimeError(index)
            latest_index[name] = index
            offsets[name].append(timestamp - index * 0.032)
            if name == 'ref':
                reference[index] = x
                reference.pop(index - 125, None)
                if timing is not None:
                    reference_timing[index] = dict(timing, captured_at_s=timestamp)
                    reference_timing.pop(index - 125, None)
            if processor is None:
                # A retry starts both arrays at one global capture index. The
                # slower stream may still deliver frames from the first try.
                # Keep its clock/reference updates above, but exclude those
                # frames from the new fit; never renumber either live stream.
                if index < recorded_start:
                    continue
                if index != recorded_start + len(recorded[name]):
                    raise RuntimeError('AEC calibration capture sequence gap')
                recorded[name].append(x)
                first_warm = (monotonic() - origin > 3
                              and min(map(len, recorded.values())) >= 75)
                retry_warm = (calibration_attempt == 2
                              and min(map(len, recorded.values())) >= 1)
                if playback_started is None and (first_warm or retry_warm):
                    playback_started = monotonic()
                    Thread(target=play_calibration, daemon=True).start()
                if (playback_started is None or not playback_done.is_set()
                        or monotonic() - playback_started < len(calibration_audio) / 16000 + 1.0):
                    continue
                anchors = {n: float(np.median(v)) for n, v in offsets.items()}
                mic, ref = (np.concatenate(recorded[n]) for n in ('mic', 'ref'))
                calibration_begin = monotonic()
                try:
                    calibration = calibrate(mic, ref, anchors['mic'] - anchors['ref'])
                except ValueError:
                    if calibration_attempt != 1:
                        raise
                    # Some capture paths settle during the first stimulus.
                    # Reuse the open captures and warm output for one new fit,
                    # under exactly the same reliability checks. The existing
                    # one-second playback tail is already the retry gap.
                    recorded_start = max(latest_index.values()) + 1
                    recorded = {n: [] for n in ('mic', 'ref')}
                    calibration_attempt = 2
                    playback_started = None
                    playback_done.clear()
                    trace.record('calibration_retry', dict(
                        calibration_attempt_index=1,
                        next_recording_start_seq=recorded_start,
                        calibration_fit_s=monotonic()-calibration_begin,
                        exception_type='ValueError'), force=True)
                    del mic, ref
                    continue
                trace.record('calibration_accepted', dict(
                    calibration_attempt_index=calibration_attempt,
                    recording_start_seq=recorded_start,
                    mic_end_seq=latest_index['mic'], ref_end_seq=latest_index['ref'],
                    calibration_fit_s=monotonic()-calibration_begin), force=True)
                if trace.enabled:
                    model_begin = monotonic()
                processor = EchoProcessor(gain=calibration['microphone_gain'], neural_models=models,
                    retain_recovery=bool(config.get('retain_recovery', False)))
                if trace.enabled:
                    training_begin = monotonic()
                shift = calibration['lag_samples'] - round(calibration['reference_lead_s'] * 16000)
                aligned = np.pad(ref, (shift, len(mic)))[:len(mic)]
                processor.process_array(mic, aligned)
                calibration['recognition_audio'] = 'aligned_microphone'
                recorded.clear()
                # Training consumes wall time; queued capture is processed in
                # order. Readiness follows catch-up, not just model training.
                live_after = monotonic()
                priming = StartupPriming(live_after)
                if trace.enabled:
                    trace.record('worker_priming', dict(calibration_fit_s=model_begin-calibration_begin,
                        model_initialization_s=training_begin-model_begin,
                        history_training_s=live_after-training_begin,
                        live_after_s=live_after, event_queue_depth=queue_depth(events)), force=True)
                continue
            if name != 'mic':
                continue
            # A changed source clock means that the calibrated delay is invalid.
            if index % 32 == 0:
                track_capture_clock(anchors, float(
                    np.median(offsets['mic']) - np.median(offsets['ref'])))
            delta = round((anchors['mic'] - anchors['ref'] - calibration['reference_delay_s']) * 16000)
            block, offset = divmod(index * 512 + delta, 512)
            if block not in reference or block + 1 not in reference:
                raise RuntimeError('AEC reference gap')
            far = np.concatenate([reference[block], reference[block + 1]])[offset:offset + 512]
            if timing is not None:
                timing.update(mic_seq=index, ref_index=block, ref_next_index=block+1,
                    ref_offset=offset, frame_samples=len(x), captured_at_s=timestamp,
                    reference_delay_s=calibration['reference_delay_s'])
                paired = reference_timing.get(block, {})
                for key in ('captured_at_s', 'pulse_latency_s', 'capture_read_s',
                            'latency_query_s', 'worker_event_wait_s'):
                    if key in paired:
                        timing['ref_' + key] = paired[key]
                dsp_begin = monotonic()
                dsp_cpu_begin = thread_time()
            clean = processor.process_array(x, far)
            if timing is not None:
                timing.update(dsp_begin_s=dsp_begin, dsp_end_s=monotonic(),
                    dsp_thread_cpu_s=thread_time()-dsp_cpu_begin,
                    reference_hold=processor.reference_hold,
                    processor_active=processor.active)
                timing['dsp_s'] = timing['dsp_end_s'] - dsp_begin
                latest_timing = timing
            if not priming.ready and timestamp < priming.live_after:
                priming.accept(timestamp, monotonic())
                continue
            # Keep the normal AEC3 waveform for local ASR. The neural mask only
            # decides voice activity; it must not erase syllables from decoding.
            pcm = (processor.asr_output * 32768).clip(-32768, 32767).astype('<i2').tobytes()
            vad_pcm = (clean * 32768).clip(-32768, 32767).astype('<i2').tobytes()
            payload = (pcm, vad_pcm)
            if processor.retain_recovery:
                recovery_pcm = (processor.recovery_output * 32768).clip(
                    -32768, 32767).astype('<i2').tobytes()
                payload += (recovery_pcm,)
            first_live = not priming.ready
            publish_at = monotonic()
            if not priming.accept(timestamp, publish_at):
                continue
            if first_live:
                # PCM is already prepared. Announce readiness and enqueue that
                # same fresh frame consecutively, without restamping its clock.
                output.put(('ready', calibration, publish_at), timeout=0.5)
            message = ('audio', payload, timestamp)
            if timing is not None:
                timing.update(worker_publish_begin_s=monotonic(),
                    previous_output_put_s=previous_put_s,
                    previous_output_put_seq=None if previous_put_s is None else index-1,
                    output_queue_depth=queue_depth(output))
                timing['after_dsp_to_publish_s'] = (
                    timing['worker_publish_begin_s'] - timing['dsp_end_s'])
                # Copy once before handing the object to multiprocessing's
                # asynchronous feeder. Do not mutate metadata after put().
                message += (dict(timing),)
            try:
                output.put(message, timeout=0.2)
            finally:
                if timing is not None:
                    ended = monotonic()
                    previous_put_s = ended - timing['worker_publish_begin_s']
                    trace.record('worker_chunk', dict(timing,
                        worker_publish_end_s=ended, output_put_s=previous_put_s,
                        worker_age_s=ended-timestamp), slow=ended-timestamp > 0.2)
            if first_live:
                trace.record('worker_ready', dict(first_audio_mic_seq=index,
                    first_audio_age_s=publish_at-timestamp,
                    priming_elapsed_s=publish_at-priming.live_after,
                    priming_historical_frames=priming.historical_frames,
                    priming_backlog_frames=priming.backlog_frames,
                    frame_samples=512), force=True)
    except Exception as error:
        failure_timing = dict(latest_timing,
            exception_type=type(error).__name__, failure_reason='worker_failure')
        if priming is not None:
            failure_timing.update(priming_elapsed_s=monotonic()-priming.live_after,
                priming_historical_frames=priming.historical_frames,
                priming_backlog_frames=priming.backlog_frames)
        trace.record('worker_failure', failure_timing, force=True)
        try:
            output.put(('error', type(error).__name__ + ': ' + str(error), monotonic()), timeout=0.1)
        except Exception:
            pass
    finally:
        local_stop.set()
        trace.close()
        # The parent has a hard termination deadline if a native read hangs.
        for thread in threads:
            thread.join(timeout=0.1)
        if warm is not None:
            warm.abort()
            warm.close()


def run_raw(source, output, stop):
    """Read raw PCM only after the dialogue owner has closed duplex input.

    Closing a native PortAudio stream concurrently with a blocking read caused
    ALSA shutdown errors on the robot. Keep this fallback in the same bounded
    process boundary as the AEC capture instead.
    """
    capture = None
    try:
        os.environ['OPENBLAS_NUM_THREADS'] = '1'
        from .pulse import Capture
        capture = Capture(source, hop=512)
        output.put(('ready', None, monotonic()), timeout=0.5)
        while not stop.is_set():
            samples, _, _ = capture.read()
            pcm = (samples * 32768).clip(-32768, 32767).astype('<i2').tobytes()
            # Raw fallback uses the legacy device-delay/one-second tail gate.
            # Detect IPC backlog here, not Pulse's cold-capture latency (which
            # can initially exceed the duplex processing deadline).
            output.put(('audio', (pcm, pcm), monotonic()), timeout=0.2)
    except Exception as error:
        try:
            output.put(('error', type(error).__name__ + ': ' + str(error), monotonic()), timeout=0.1)
        except Exception:
            pass
    finally:
        if capture is not None:
            capture.close()
