"""Read XFM/R818 direction events without STT or audio capture."""

# Current firmware reports hardware wake angles, not continuous sound direction.
# Only acknowledge packets; never select a beam or change microphone firmware.

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
        """Start with an empty protocol buffer."""
        self.buffer = bytearray()

    def feed(self, data):
        """Return complete valid frames and retain only a bounded partial frame."""
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


class Directions:
    """Consume one fresh hardware direction once, independently of speech."""

    def __init__(self):
        """Bound the pending measurements and protect reader access."""
        self.samples = deque(maxlen=16)
        self.lock = Lock()

    def add(self, angle, at):
        """Buffer a measurement using the local monotonic reception time."""
        with self.lock:
            self.samples.append((at, angle))

    def take(self, now, *, forward_degrees=150.):
        """Drop stale/ambiguous events and map the measured axis to robot yaw."""
        with self.lock:
            samples = list(self.samples)
            self.samples.clear()
        if len(samples) != 1 or not math.isfinite(now):
            return None
        at, angle = samples[0]
        if (not math.isfinite(at) or not 0 <= now - at <= 1.
                or not math.isfinite(angle) or not 0 <= angle < 360):
            return None
        return at, math.radians((angle - forward_degrees + 180) % 360 - 180)


class SerialDirections(Directions):
    """One sleeping reader; only acknowledge packets, never modify firmware."""

    def __init__(self, port, *, clock=monotonic):
        """Open the existing serial port exclusively without audio capture."""
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
            pass  # The controller observes disconnected state and cancels its own turn.
        finally:
            self.connected = False
            with self.lock:
                self.samples.clear()
            self.serial.close()

    def take(self, *args, **kwargs):
        """Return measurements only while the serial source remains connected."""
        return super().take(*args, **kwargs) if self.connected else None

    def close(self):
        """Release the port and join the bounded reader thread."""
        self.stop.set()
        self.thread.join(timeout=1.)
        self.serial.close()
