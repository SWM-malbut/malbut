"""Read the existing XFM/R818 wake-angle wire protocol without capturing audio.

The board emits angles for its own wake words, not for arbitrary speech. Never
manufacture an angle by sending manual_wakeup (that command selects a beam).
"""

from collections import deque
import json
import math
from threading import Event, Lock, Thread
from time import monotonic


def packet(kind, payload, identity=b'\0\0'):
    """Encode the vendor's length-delimited, checksummed serial frame."""
    data = b'\xa5\x01' + bytes([kind]) + len(payload).to_bytes(2, 'little') + identity + payload
    return data + bytes([-sum(data) & 255])


class DirectionFrames:
    """Bound buffering and recover after partial, corrupt or oversized packets."""

    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data[:4096])
        frames = []
        while len(self.buffer) >= 8:
            if self.buffer[:2] != b'\xa5\x01':
                del self.buffer[0]
                continue
            length = int.from_bytes(self.buffer[3:5], 'little') + 8
            if length > 8192:
                del self.buffer[0]
                continue
            if len(self.buffer) < length:
                break
            frame = bytes(self.buffer[:length])
            if sum(frame) & 255:
                del self.buffer[0]
                continue
            del self.buffer[:length]
            frames.append((frame[2], frame[5:7], frame[7:-1]))
        return frames


def wake_angle(payload):
    """Extract only a real hardware wake event, including nested JSON firmware."""
    try:
        def unpack(value):
            return json.loads(value) if isinstance(value, str) else value

        message = json.loads(payload)
        content = unpack(message['content'])
        if message['type'] != 'aiui_event' or content['eventType'] != 4:
            return None
        info = unpack(content['info'])
        result = unpack(info.get('ivw', info))
        angle = result['angle']
        if type(angle) not in (int, float) or not math.isfinite(angle) or not 0 <= angle < 360:
            return None
        return float(angle)
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError):
        return None


class WakeDirections:
    """Consume a single direction only from the matched utterance's audio window."""

    def __init__(self):
        self.samples = deque(maxlen=16)
        self.lock = Lock()

    def add(self, angle, at):
        with self.lock:
            self.samples.append((at, angle))

    def take(self, window, now, *, capture_delay=.5, forward_degrees=150.):
        # 150 degrees is the raw forward axis derived from ROSOrin's existing
        # conversions: raw -> (240-raw)%360 -> yaw = 90-vendor_angle.
        with self.lock:
            samples = list(self.samples)
            self.samples.clear()  # No reuse on a second call, including rejected calls.
        if window is None or not all(math.isfinite(v) for v in (*window, now)):
            return None
        start, end = window
        if not start <= end <= now or now - end > 3.:
            return None
        matches = [(at, angle) for at, angle in samples
                   if start - capture_delay - .15 <= at <= end - capture_delay + .15
                   and 0 <= now - at <= 4.]
        # Multiple wake events can belong to different people; don't guess.
        if len(matches) != 1:
            return None
        at, angle = matches[0]
        yaw = math.radians((angle - forward_degrees + 180) % 360 - 180)
        return at, yaw


class SerialDirections(WakeDirections):
    """One sleeping reader; only acknowledge packets, never modify firmware."""

    def __init__(self, port, *, clock=monotonic):
        super().__init__()
        import serial

        self.clock = clock
        self.stop = Event()
        self.connected = False
        self.serial = serial.Serial(None, 115200, timeout=.2, write_timeout=.2, exclusive=True)
        self.serial.rts = self.serial.dtr = False
        self.serial.port = port
        self.serial.open()
        self.serial.reset_input_buffer()  # Discard packets queued before this process existed.
        self.connected = True
        self.thread = Thread(target=self._run, name='xfm-direction', daemon=True)
        self.thread.start()

    def _run(self):
        decoder = DirectionFrames()
        recent = deque(maxlen=32)
        try:
            while not self.stop.is_set():
                data = self.serial.read(min(max(self.serial.in_waiting, 1), 4096))
                for kind, identity, payload in decoder.feed(data):
                    if kind in (1, 4):
                        self.serial.write(packet(0xff, b'\xa5\0\0\0', identity))
                    if kind != 4:
                        continue
                    # Retransmissions are acknowledged but must not become new observations.
                    current = (identity, payload)
                    if current in recent:
                        continue
                    recent.append(current)
                    angle = wake_angle(payload)
                    if angle is not None:
                        self.add(angle, self.clock())
        except (OSError, ValueError):
            pass  # Direction loss must not terminate ordinary microphone listening.
        finally:
            self.connected = False
            with self.lock:
                self.samples.clear()
            self.serial.close()

    def take(self, *args, **kwargs):
        return super().take(*args, **kwargs) if self.connected else None

    def close(self):
        self.stop.set()
        self.thread.join(timeout=1.)
        self.serial.close()
