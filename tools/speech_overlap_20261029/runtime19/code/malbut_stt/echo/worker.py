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
    phase = None
    threads = []
    local_stop = Event()
    try:
        os.environ['OPENBLAS_NUM_THREADS'] = '1'
        os.environ['OMP_NUM_THREADS'] = '1'
        import numpy as np
        import sounddevice as sd
        from .processor import EchoProcessor, calibrate
        from .spatial_echo_gate import SpatialEchoGate
        from .quiet_asr import QuietAsrRecovery
        from .pulse import Capture
        from .startup_phase import PhaseClient

        record_prefix = os.environ.get('MALBUT_RETEST_RECORD_PREFIX')
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
        phase = PhaseClient(os.environ['MALBUT_SPATIAL_PHASE_DIR'], config['calibration_audio'])
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
                cap = Capture(device, hop=512, channels=2 if name == 'mic' else 1)
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
        recorded_timing = []
        reference = {}
        deferred_events = deque()
        origin = monotonic()
        playback_started = None
        playback_error = []
        playback_done = Event()
        processor = anchors = calibration = None
        phase_warm_until = None
        phase_boundary = None
        spatial_gate = SpatialEchoGate(np.load(Path(__file__).resolve().parents[3] / 'diverse_spatial/leakage.npy'))
        gate_delay = np.zeros(128, np.float32)
        asr_recovery = QuietAsrRecovery()
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
                name, index, x, timestamp = (deferred_events.popleft() if deferred_events
                                             else events.get(timeout=0.5))
            except Empty:
                raise RuntimeError('AEC microphone/reference timed out')
            if name == 'error':
                raise RuntimeError(index)
            if name == 'mic':
                timing_audio = x[:, 1].copy()
                if processor is None:
                    recorded_timing.append(x[:, 1].copy())
                x = x[:, 0].copy()
            offsets[name].append(timestamp - index * 0.032)
            if name == 'ref':
                reference[index] = x
                reference.pop(index - 125, None)
            if processor is None:
                recorded[name].append(x)
                if monotonic() - origin > 3 and min(map(len, recorded.values())) >= 75 and playback_started is None:
                    phase.arm(calibration_audio)
                    playback_started = monotonic()
                    Thread(target=play_calibration, daemon=True).start()
                if (playback_started is None or not playback_done.is_set()
                        or monotonic() - playback_started < len(calibration_audio) / 16000 + 1.0):
                    continue
                anchors = {n: float(np.median(v)) for n, v in offsets.items()}
                mic, ref = (np.concatenate(recorded[n]) for n in ('mic', 'ref'))
                calibration = calibrate(np.concatenate(recorded_timing), ref, anchors['mic'] - anchors['ref'])
                calibration['timing_input'] = 'raw microphone 2 with matched spatial delay'
                shift = calibration['lag_samples'] - round(calibration['reference_lead_s'] * 16000)
                aligned = np.pad(ref, (shift, len(mic)))[:len(mic)]
                if record_prefix:
                    # Preserve the actual reference even if phase selection rejects startup.
                    np.savez(record_prefix + '_training.npz', mic=mic,
                        timing=np.concatenate(recorded_timing), far=aligned, reference=ref,
                        gain=calibration['microphone_gain'], mic_anchor=anchors['mic'],
                        reference_anchor=anchors['ref'], calibration_json=json.dumps(calibration))
                selection = phase.select(np.concatenate(recorded_timing), ref, calibration_audio,
                    Path(__file__).resolve().parents[3] / 'diverse_spatial/phase_reference.npz')
                calibration['spatial_phase'] = dict(delays=selection['delays'], **selection['evidence'])
                processor = EchoProcessor(gain=calibration['microphone_gain'], neural_models=models)
                if any(selection['delays']):
                    a, b = selection['training_interval']
                    processor.process_array(selection['corrected_training'], aligned[a:b])
                    phase_boundary = selection['boundary']
                    phase_warm_until = ((phase_boundary + 511) // 512 + 1) * 512
                else:
                    processor.process_array(mic, aligned)
                del selection
                calibration['recognition_audio'] = 'aligned spatial microphone'
                recorded_timing.clear()
                if phase_warm_until is None:
                    output.put(('ready', calibration, monotonic()), timeout=0.5)
                recorded.clear()
                # Training consumes wall time; queued capture is processed in
                # order but not published until it catches up to live audio.
                live_after = monotonic()
                continue
            if name != 'mic':
                continue
            if phase_boundary is not None and index * 512 < phase_boundary:
                # Drop the straddling block too; never publish pre-selection PCM.
                continue
            # A changed source clock means that the calibrated delay is invalid.
            if index % 32 == 0:
                track_capture_clock(anchors, float(
                    np.median(offsets['mic']) - np.median(offsets['ref'])))
            delta = round((anchors['mic'] - anchors['ref'] - calibration['reference_delay_s']) * 16000)
            block, offset = divmod(index * 512 + delta, 512)
            # The low-latency ADC can arrive before the matching render block.
            # Retain microphone events while waiting for at most two capture hops.
            reference_deadline = monotonic() + 0.064
            while block not in reference or block + 1 not in reference:
                if reference and block < min(reference):
                    raise RuntimeError('AEC reference expired')
                remaining = reference_deadline - monotonic()
                if remaining <= 0:
                    raise RuntimeError('AEC reference gap')
                try:
                    pending = events.get(timeout=remaining)
                except Empty:
                    raise RuntimeError('AEC reference gap') from None
                if pending[0] == 'error':
                    raise RuntimeError(pending[1])
                if pending[0] == 'ref':
                    _, ref_index, ref_audio, ref_timestamp = pending
                    offsets['ref'].append(ref_timestamp - ref_index * 0.032)
                    reference[ref_index] = ref_audio
                    reference.pop(ref_index - 125, None)
                else:
                    deferred_events.append(pending)
            far = np.concatenate([reference[block], reference[block + 1]])[offset:offset + 512]
            if record_prefix:
                with open(record_prefix + '_pair.f32','ab') as saved:
                    saved.write(np.column_stack((x,far)).astype('<f4').tobytes())
                with open(record_prefix + '_stamps.f64','ab') as saved:
                    saved.write(np.asarray([monotonic()],dtype='<f8').tobytes())
                with open(record_prefix + '_timing.f32', 'ab') as saved:
                    saved.write(timing_audio.astype('<f4').tobytes())
            processor.process_array(x, far)
            recognition = asr_recovery.process(processor.recovery_output, timing_audio, far)
            gated = np.concatenate([spatial_gate.process(x[j:j+128], timing_audio[j:j+128]) for j in range(0, 512, 128)])
            clean = np.concatenate((gate_delay, gated))[:512]
            gate_delay = gated[-128:].copy()
            if record_prefix:
                with open(record_prefix + '_stages.f32', 'ab') as saved:
                    saved.write(np.column_stack((recognition, clean, processor.asr_output)).astype('<f4').tobytes())
            if phase_warm_until is not None:
                if (index + 1) * 512 >= phase_warm_until:
                    output.put(('ready', calibration, monotonic()), timeout=0.5)
                    phase_warm_until = None
                    live_after = monotonic()
                continue
            if timestamp < live_after:
                continue
            # Keep ASR continuous; use raw speech only after the echo tail drains.
            # VAD remains on the independently gated spatial signal.
            pcm = (recognition * 32768).clip(-32768, 32767).astype('<i2').tobytes()
            vad_pcm = (clean * 32768).clip(-32768, 32767).astype('<i2').tobytes()
            recovery_pcm = pcm
            output.put(('audio', (pcm, vad_pcm, recovery_pcm), timestamp), timeout=0.2)
    except Exception as error:
        try:
            output.put(('error', type(error).__name__ + ': ' + str(error), monotonic()), timeout=0.1)
        except Exception:
            pass
    finally:
        if phase is not None:
            phase.close()
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
