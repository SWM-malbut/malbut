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
from time import monotonic
import wave


def track_capture_clock(anchors, observed_difference):
    """Follow slow device clock drift; reject discontinuities instead of guessing."""
    drift = observed_difference - (anchors['mic'] - anchors['ref'])
    if not math.isfinite(drift) or abs(drift) > 0.04:
        raise RuntimeError('AEC reference clock changed')
    # At most 1 ms per second, within the linear filter's causal tail.
    anchors['mic'] += max(-0.001, min(0.001, drift))


def run(config, output, stop):
    if 'raw_source' in config:
        run_raw(config['raw_source'], output, stop)
        return
    warm = None
    threads = []
    local_stop = Event()
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
                cap = Capture(device, hop=512)
                index = 0
                while not stop.is_set() and not local_stop.is_set():
                    x, timestamp, _ = cap.read()
                    events.put((name, index, x, timestamp), timeout=0.2)
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
        reference = {}
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
            if local_stop.is_set():
                raise RuntimeError('AEC capture worker failed')
            if playback_error:
                raise RuntimeError('Calibration playback failed')
            try:
                name, index, x, timestamp = events.get(timeout=0.5)
            except Empty:
                raise RuntimeError('AEC microphone/reference timed out')
            if name == 'error':
                raise RuntimeError(index)
            offsets[name].append(timestamp - index * 0.032)
            if name == 'ref':
                reference[index] = x
                reference.pop(index - 125, None)
            if processor is None:
                recorded[name].append(x)
                if monotonic() - origin > 3 and min(map(len, recorded.values())) >= 75 and playback_started is None:
                    playback_started = monotonic()
                    Thread(target=play_calibration, daemon=True).start()
                if (playback_started is None or not playback_done.is_set()
                        or monotonic() - playback_started < len(calibration_audio) / 16000 + 1.0):
                    continue
                anchors = {n: float(np.median(v)) for n, v in offsets.items()}
                mic, ref = (np.concatenate(recorded[n]) for n in ('mic', 'ref'))
                calibration = calibrate(mic, ref, anchors['mic'] - anchors['ref'])
                processor = EchoProcessor(gain=calibration['microphone_gain'], neural_models=models)
                shift = calibration['lag_samples'] - round(calibration['reference_lead_s'] * 16000)
                aligned = np.pad(ref, (shift, len(mic)))[:len(mic)]
                processor.process_array(mic, aligned)
                calibration['recognition_audio'] = 'aligned_microphone'
                output.put(('ready', calibration, monotonic()), timeout=0.5)
                recorded.clear()
                # Training consumes wall time; queued capture is processed in
                # order but not published until it catches up to live audio.
                live_after = monotonic()
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
            clean = processor.process_array(x, far)
            if timestamp < live_after:
                continue
            # Keep the normal AEC3 waveform for local ASR. The neural mask only
            # decides voice activity; it must not erase syllables from decoding.
            pcm = (processor.asr_output * 32768).clip(-32768, 32767).astype('<i2').tobytes()
            vad_pcm = (clean * 32768).clip(-32768, 32767).astype('<i2').tobytes()
            recovery_pcm = (processor.recovery_output * 32768).clip(-32768, 32767).astype('<i2').tobytes()
            output.put(('audio', (pcm, vad_pcm, recovery_pcm), timestamp), timeout=0.2)
    except Exception as error:
        try:
            output.put(('error', type(error).__name__ + ': ' + str(error), monotonic()), timeout=0.1)
        except Exception:
            pass
    finally:
        local_stop.set()
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
