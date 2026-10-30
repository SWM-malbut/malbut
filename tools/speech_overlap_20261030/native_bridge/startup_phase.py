"""One startup-only phase decision; no diagnostic PCM files or live adaptation."""
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
from threading import Event, Lock, Thread
from time import monotonic
import uuid

import numpy as np

PROBE_SHA256 = '258cc14eca86ec6be6936c270e5ecca067a51fc013355b78a400d1ed3ee17a3d'
RATE = 16000
MAX_FRAMES = 312 * 512  # 9.984 seconds, only while the known startup probe runs.
MAX_PAYLOAD = MAX_FRAMES * 6 * 2


def _receive(sock, size):
    pieces = []
    while size:
        data = sock.recv(size)
        if not data:
            raise RuntimeError('Startup phase peer closed before completion')
        pieces.append(data); size -= len(data)
    return b''.join(pieces)


def _send(sock, message, payload=b''):
    header = json.dumps(message, separators=(',', ':')).encode()
    if len(header) > 8192 or len(payload) > MAX_PAYLOAD:
        raise ValueError('Startup phase packet exceeds bound')
    sock.sendall(struct.pack('!II', len(header), len(payload)) + header + payload)


def _read(sock):
    header, count = struct.unpack('!II', _receive(sock, 8))
    if not 0 < header <= 8192 or count > MAX_PAYLOAD:
        raise ValueError('Invalid startup phase packet size')
    return json.loads(_receive(sock, header)), _receive(sock, count)


def _valid_delays(delays):
    return (type(delays) is list and len(delays) == 6 and delays[:2] == [0, 0]
            and all(type(x) in (int, float) and np.isfinite(x) and -1 <= x <= 1 for x in delays))


def _delay(raw, previous, delays):
    result = raw.astype('f4') / 32768
    channels = np.flatnonzero(np.asarray(delays) >= 1)
    result[1:, channels] = result[:-1, channels].copy()
    result[0, channels] = np.asarray(previous, dtype='f4')[channels] / 32768
    return result


def _phase_weights(weights, delays):
    # Positive integer delays stay in PCM; signed residuals rotate the fixed filter weights.
    fraction = np.asarray(delays) - np.maximum(np.floor(delays), 0)
    if not np.any(fraction):
        return weights
    return weights * np.exp(2j * np.pi * np.fft.rfftfreq(512)[:, None] * fraction)


def _match(signal, template):
    """Bounded normalized correlation, excluding numerically empty windows."""
    signal = np.asarray(signal, dtype='f8'); template = np.asarray(template, dtype='f8')
    if len(template) < RATE or len(signal) < len(template):
        raise ValueError('Insufficient startup probe samples')
    if (len(signal) > RATE*16 or not np.isfinite(signal).all()
            or not np.isfinite(template).all() or template @ template <= 1e-12):
        raise ValueError('Invalid startup probe PCM')
    size = 1 << (len(signal) + len(template) - 2).bit_length()
    values = np.fft.irfft(np.fft.rfft(signal, size) * np.conj(np.fft.rfft(template, size)), size)
    values = values[:len(signal) - len(template) + 1]
    sums = np.cumsum(np.r_[0., signal * signal]); energy = sums[len(template):] - sums[:-len(template)]
    valid = energy > max(float(energy.max()) * 1e-6, 1e-12)
    score = np.zeros_like(values)
    score[valid] = values[valid] / np.sqrt(energy[valid] * float(template @ template))
    index = int(np.argmax(abs(score)))
    return index, float(abs(score[index]))


def probe_window(timing, reference, probe):
    """Require the actual render and microphone to contain the pinned probe."""
    found = {}
    for kind, signal, minimum, spread in [('render', reference, .95, 16), ('mic', timing, .06, 320)]:
        matches = []
        for begin, end in [(0, len(probe)//2), (len(probe)//2, len(probe))]:
            index, score = _match(signal, probe[begin:end])
            matches.append((index - begin, score))
        if min(x[1] for x in matches) < minimum or abs(matches[0][0]-matches[1][0]) > spread:
            raise ValueError('Known startup probe is not stable in ' + kind)
        found[kind] = matches
    mic_start = round(np.mean([x[0] for x in found['mic']]))
    render_start = round(np.mean([x[0] for x in found['render']]))
    trim = 1536  # 96 ms excludes playback onset/tail without using a speech label.
    a, b = mic_start + trim, mic_start + len(probe) - trim
    ra, rb = render_start + trim, render_start + len(probe) - trim
    count = (b-a)//128*128
    b, rb = a+count, ra+count
    if min(a, ra) < 0 or b > len(timing) or rb > len(reference) or b-a < RATE*2:
        raise ValueError('Startup probe window is incomplete')
    return a, b, reference[ra:rb].copy(), found


class PhaseClient:
    def __init__(self, directory, probe_path):
        if hashlib.sha256(Path(probe_path).read_bytes()).hexdigest() != PROBE_SHA256:
            raise ValueError('Startup probe differs from the frozen calibration asset')
        self.path = Path(directory) / 'phase.sock'
        self.sock = None

    def arm(self, probe):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(2)
        self.sock.connect(str(self.path))
        _send(self.sock, dict(op='arm', probe_sha256=PROBE_SHA256, frames=len(probe)))
        reply, payload = _read(self.sock)
        if reply.get('op') != 'armed' or payload:
            raise ValueError('Invalid startup phase arm reply')
        self.nonce = reply['nonce']
        self.sock.settimeout(3)

    def select(self, timing, reference, probe, profiles):
        from .startup_phase_estimator import estimate
        a, b, rendered, matches = probe_window(timing, reference, probe)
        exact = np.rint(timing[a:b] * 32768).astype('<i2')
        if not np.array_equal(exact.astype('f4')/32768, timing[a:b]):
            raise ValueError('Raw timing channel is not exact microphone PCM16')
        _send(self.sock, dict(op='window', nonce=self.nonce, capture_start=a, frames=b-a), exact.tobytes())
        reply, payload = _read(self.sock)
        if reply.get('op') != 'window' or reply.get('nonce') != self.nonce or len(payload) != (b-a)*12:
            raise ValueError('Invalid startup raw window reply')
        raw = np.frombuffer(payload, '<i2').reshape(-1, 6).astype('f4')/32768
        record_prefix = os.environ.get('MALBUT_RETEST_RECORD_PREFIX')
        diagnostic = dict(status='selecting', capture_interval=[a,b],
                          raw_start=reply.get('raw_start'), probe_matches=matches)
        if record_prefix:
            np.savez(record_prefix + '_phase_input.npz', raw6=raw, render=rendered,
                     timing=timing[a:b], capture_interval=[a,b], raw_start=reply.get('raw_start',-1))
        try:
            decision = estimate(raw, rendered, profiles)
        except Exception as error:
            if record_prefix:
                diagnostic.update(status='rejected', error=type(error).__name__+': '+str(error),
                                  rejection=getattr(error,'evidence',None))
                Path(record_prefix + '_phase.json').write_text(json.dumps(diagnostic,indent=2))
            raise
        if record_prefix:
            diagnostic.update(status='selected', decision=decision)
            Path(record_prefix + '_phase.json').write_text(json.dumps(diagnostic,indent=2))
        if not _valid_delays(decision.get('delays')):
            raise ValueError('Invalid startup phase decision')
        _send(self.sock, dict(op='lock', nonce=self.nonce, delays=decision['delays']))
        reply, payload = _read(self.sock)
        if (reply.get('op') != 'locked' or reply.get('nonce') != self.nonce
                or not _valid_delays(reply.get('delays')) or reply['delays'] != decision['delays']):
            raise ValueError('Invalid startup phase lock reply')
        if len(payload) != ((b-a)*4 if any(decision['delays']) else 0):
            raise ValueError('Invalid corrected training window')
        self.close()
        return dict(delays=decision['delays'], boundary=reply['capture_boundary'],
                    corrected_training=np.frombuffer(payload, '<f4').copy() if payload else None,
                    training_interval=(a, b), evidence=dict(probe=matches, phase=decision['evidence']))

    def close(self):
        if self.sock is not None:
            self.sock.close(); self.sock = None


class PhaseBridge:
    """Own the bounded startup ring and apply exactly one decision on the audio thread."""
    def __init__(self, directory, weights, filter_factory, on_failure=lambda: None):
        directory = Path(directory)
        if not directory.is_dir() or directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
            raise ValueError('Startup phase directory must be private and owned by this user')
        self.path = directory/'phase.sock'
        self.weights = weights; self.filter_factory = filter_factory
        self.on_failure = on_failure
        self.lock = Lock(); self.applied = Event(); self.closed = Event()
        self.history = deque(maxlen=3); self.next_frame = 0
        self.previous = np.zeros(6, '<i2'); self.sealed = False
        self.pending = None; self.delays = None; self.failure = None
        self.deadline = monotonic() + 60
        self.nonce = uuid.uuid4().hex; self.boundary = None
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.path)); os.chmod(self.path, 0o600)
        self.listener.listen(1); self.listener.settimeout(60)
        self.thread = Thread(target=self._serve, daemon=True); self.thread.start()

    def _serve(self):
        try:
            with self.listener.accept()[0] as peer:
                if hasattr(socket, 'SO_PEERCRED'):
                    _, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if uid != os.getuid(): raise ValueError('Unexpected startup phase peer')
                peer.settimeout(2)
                message, data = _read(peer)
                frames = message.get('frames')
                if (message.get('op') != 'arm' or data or message.get('probe_sha256') != PROBE_SHA256
                        or type(frames) is not int or not RATE*2 <= frames <= RATE*8):
                    raise ValueError('Invalid startup phase arm')
                with self.lock:
                    self.history = deque(self.history, maxlen=312)
                    self.deadline = monotonic() + frames/RATE + 4
                _send(peer, dict(op='armed', nonce=self.nonce))
                peer.settimeout(frames/RATE + 3)
                message, data = _read(peer)
                count = message.get('frames'); start = message.get('capture_start')
                if (message.get('op') != 'window' or message.get('nonce') != self.nonce
                        or type(count) is not int or not RATE*2 <= count <= RATE*8 or count % 128
                        or type(start) is not int or not 0 <= start <= RATE*16 or len(data) != count*2):
                    raise ValueError('Invalid startup phase window')
                with self.lock:
                    raw = np.concatenate([x[1] for x in self.history])
                    origin = self.history[0][0]
                    self.history = deque(list(self.history)[-3:], maxlen=3)
                channel = raw[:, 1].tobytes(); location = channel.find(data)
                if location < 0 or location % 2 or channel.find(data, location+1) >= 0:
                    raise ValueError('Startup microphone window is absent or ambiguous')
                begin = location//2; end = begin+count
                tail = ((origin+end+384+127)//128)*128-origin
                if begin < 1025 or tail > len(raw):
                    raise ValueError('Startup microphone window lacks filter context')
                # Preserve the bridge's global128sample STFT hop phase as well.
                context = ((origin+begin-1024)//128)*128-origin
                if context < 1: raise ValueError('Missing startup filter seed')
                training = raw[context-1:tail].copy()
                training_offset = begin-context+384
                offset = origin + begin + 384 - start
                _send(peer, dict(op='window', nonce=self.nonce, raw_start=origin+begin), raw[begin:end].tobytes())
                del raw, channel
                peer.settimeout(3)
                message, data = _read(peer)
                if (message.get('op') != 'lock' or message.get('nonce') != self.nonce or data
                        or not _valid_delays(message.get('delays'))):
                    raise ValueError('Invalid startup phase lock')
                chosen = message['delays']; corrected = b''
                if any(chosen):
                    # WOLA warmup from preceding real samples; preserve exact requested alignment.
                    x = _delay(training[1:], training[0], chosen); filt = self.filter_factory(_phase_weights(self.weights, chosen))
                    pad = (-len(x)) % 128; x = np.pad(x, ((0,pad),(0,0)))
                    y = np.concatenate([(filt.process(x[i:i+128])*32).clip(-.98,.98) for i in range(0,len(x),128)])
                    corrected = y[training_offset:training_offset+count].astype('<f4').tobytes()
                with self.lock: self.pending = chosen
                if not self.applied.wait(2): raise TimeoutError('Startup phase audio boundary was not acknowledged')
                _send(peer, dict(op='locked', nonce=self.nonce, delays=chosen,
                                 capture_boundary=self.boundary-offset), corrected)
                with self.lock:
                    self.history.clear(); self.sealed = True; self.deadline = float('inf')
        except Exception as error:
            if not self.closed.is_set():
                self.failure = error
                try: self.on_failure()
                except ProcessLookupError: pass
        finally:
            self.listener.close()
            try: self.path.unlink()
            except FileNotFoundError: pass

    def prepare(self, processor, raw, frame):
        """Called before the current raw block is added; history contains only past PCM."""
        if self.failure is not None: raise RuntimeError('Startup phase failed: '+str(self.failure))
        if monotonic() >= self.deadline: raise TimeoutError('Startup phase deadline exceeded')
        with self.lock:
            if frame != self.next_frame: raise ValueError('Non-contiguous startup raw input')
            previous = self.previous
            if self.pending is not None:
                if self.delays is not None: raise ValueError('Startup phase was already locked')
                self.delays = self.pending; self.pending = None; self.boundary = frame
                if any(self.delays):
                    history = np.concatenate([x[1] for x in self.history])
                    if len(history) < 1025: raise ValueError('Missing startup phase warmup')
                    warm = _delay(history[-1024:], history[-1025], self.delays); processor = self.filter_factory(_phase_weights(self.weights, self.delays))
                    for i in range(0,1024,128): processor.process(warm[i:i+128])
            shifted = _delay(raw, previous, self.delays) if self.delays is not None and any(self.delays) else raw.astype('f4')/32768
            self.previous = raw[-1].copy()
            if not self.sealed: self.history.append((frame, raw.copy()))
            self.next_frame = frame+len(raw)
        return processor, shifted

    def written(self, end_frame):
        if self.boundary is not None and not self.applied.is_set() and end_frame >= self.boundary+512:
            self.applied.set()

    def close(self):
        self.closed.set(); self.listener.close()
        try: self.path.unlink()
        except FileNotFoundError: pass
