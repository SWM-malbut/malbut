"""Validate hardware direction framing and freshness without STT or firmware writes."""

import json
import math

import pytest

from malbut_system_manager.sound_direction import DirectionFrames, Directions, packet, wake_angle


def event(angle, *, nested=False):
    """Encode a documented firmware direction event."""
    info = {'ivw': {'angle': angle, 'keyword': 'test'}}
    content = {'eventType': 4, 'info': json.dumps(info) if nested else info}
    return json.dumps({'type': 'aiui_event',
                       'content': json.dumps(content) if nested else content}).encode()


@pytest.fixture
def direction_logs(caplog):
    """Capture this logger independently of logging changes in other ROS tests."""
    import logging
    from malbut_system_manager.sound_direction import LOGGER

    handlers, level = LOGGER.handlers[:], LOGGER.level
    propagate, disabled = LOGGER.propagate, LOGGER.disabled
    LOGGER.handlers = [caplog.handler]
    LOGGER.setLevel(logging.WARNING)
    LOGGER.propagate = LOGGER.disabled = False
    try:
        yield caplog
    finally:
        LOGGER.handlers = handlers
        LOGGER.setLevel(level)
        LOGGER.propagate, LOGGER.disabled = propagate, disabled


@pytest.mark.parametrize('nested', [False, True])
def test_fragmented_hardware_event_and_nested_firmware_json(nested):
    """Fragmented hardware event and nested firmware json."""
    wire = packet(4, event(240, nested=nested), b'\x11\0')
    parser = DirectionFrames()
    frames = []
    for byte in wire:
        frames.extend(parser.feed(bytes([byte])))
    assert frames == [(4, b'\x11\0', event(240, nested=nested))]
    assert wake_angle(frames[0][2]) == 240


def test_corruption_oversize_and_noise_do_not_create_angles():
    """Corruption oversize and noise do not create angles."""
    valid = packet(4, event(150))
    parser = DirectionFrames()
    noise = b'garbage\xa5\x01\x04\xff\xff\0\0\0'
    broken = valid[:-1] + bytes([(valid[-1] + 1) % 256])
    assert parser.feed(noise + broken + valid) == [(4, b'\0\0', event(150))]
    for _ in range(100):
        parser.feed(bytes(4096))
    assert len(parser.buffer) < 8


@pytest.mark.parametrize('angle', [None, True, '90', -1, 360, math.inf, math.nan,
                                   10**400, -(10**400)])
def test_invalid_hardware_angle_is_not_forward(angle):
    """Invalid hardware angle is not forward."""
    assert wake_angle(event(angle)) is None


@pytest.mark.parametrize('payload', [b'', b'{}', b'[]', b'null', b'\xff',
                                     b'{"type":"version","content":"circle6"}'])
def test_non_wake_messages_are_ignored(payload):
    """Non wake messages are ignored."""
    assert wake_angle(payload) is None


@pytest.mark.parametrize('raw,yaw', [(150, 0), (240, math.pi/2), (60, -math.pi/2),
                                     (330, -math.pi), (0, -5*math.pi/6)])
def test_vendor_forward_left_right_mapping_and_consumption(raw, yaw):
    """Vendor forward left right mapping and consumption."""
    cache = Directions()
    cache.add(raw, 10.)
    assert cache.take(10.5) == (10., pytest.approx(yaw))
    assert cache.take(10.5) is None


@pytest.mark.parametrize('at', [5., 9., 11., 20.])
def test_stale_or_future_angle_is_not_reused(at):
    """Stale or future angle is not reused."""
    cache = Directions()
    cache.add(240, at)
    assert cache.take(10.5) is None


def test_multiple_events_or_late_processing_do_not_trigger_a_turn():
    """Multiple events or late processing do not trigger a turn."""
    cache = Directions()
    cache.add(240, 10.)
    cache.add(60, 10.2)
    assert cache.take(10.5) is None
    cache.add(240, 10.)
    assert cache.take(16.) is None
    cache.add(240, 10.)
    assert cache.take(math.nan) is None


@pytest.mark.parametrize('length', [4096, 8184])
def test_corrupt_in_range_length_recovers_at_valid_following_frame(length):
    """A plausible damaged length must not hide subsequent valid packets."""
    parser = DirectionFrames()
    corrupt = b'\xa5\x01\x04' + length.to_bytes(2, 'little') + b'\0\0\0'
    valid = packet(4, event(240), b'\x11\0')
    broken = valid[:-1] + bytes([(valid[-1] + 1) % 256])
    assert parser.feed_timed(corrupt + broken + valid, 10.) == [
        (4, b'\x11\0', event(240), 10.)]
    assert not parser.buffer


@pytest.mark.parametrize('split', [1, 6, 10])
def test_stalled_partial_frame_expires_even_without_new_bytes(split):
    """A partial header or payload cannot become a fresh event much later."""
    parser = DirectionFrames()
    wire = packet(4, event(240))
    assert parser.feed_timed(wire[:split], 10.) == []
    assert parser.feed_timed(b'', 11.01) == []
    assert not parser.buffer
    assert parser.feed_timed(wire[split:], 11.1) == []
    assert parser.feed_timed(wire, 11.2) == [(4, b'\0\0', event(240), 11.2)]


def test_fragmented_frame_preserves_first_byte_time_across_noise():
    """The direction timestamp predates the final fragment and JSON parsing."""
    parser = DirectionFrames()
    wire = packet(4, event(240))
    assert parser.feed_timed(b'noise' + wire[:1], 10.) == []
    assert parser.feed_timed(wire[1:], 10.4) == [(4, b'\0\0', event(240), 10.)]


def run_scripted_serial(chunks, *, ack_delay=0.):
    """Run the real reader loop synchronously against timed in-memory bytes."""
    from threading import Event
    from malbut_system_manager.sound_direction import SerialDirections

    reader = SerialDirections.__new__(SerialDirections)
    Directions.__init__(reader)
    reader.stop = Event()
    reader.connected = True
    clock = [10.]
    reader.clock = lambda: clock[0]
    observed = []

    def add(angle, at):
        observed.append((at, angle))
        Directions.add(reader, angle, at)

    reader.add = add

    class Port:
        def __init__(self):
            self.chunks = list(chunks)
            self.writes = []
            self.resets = 0
            self.sample = None
            self.closed = False

        @property
        def in_waiting(self):
            return len(self.chunks[0][1]) if self.chunks else 0

        def read(self, count):
            if self.chunks:
                clock[0], data = self.chunks.pop(0)
                assert len(data) <= count
                return data
            self.sample = reader.take(clock[0] + .1)
            reader.stop.set()
            return b''

        def write(self, data):
            self.writes.append(data)
            clock[0] += ack_delay
            return len(data)

        def reset_input_buffer(self):
            # Later scripted chunks are future arrivals, not currently queued bytes.
            self.resets += 1

        def close(self):
            self.closed = True

    reader.serial = port = Port()
    reader._run()
    port.connected = reader.connected
    return observed, port


def test_ack_delay_does_not_refresh_the_measurement_time():
    """Processing after receipt cannot make an expired observation usable."""
    observed, port = run_scripted_serial([(10., packet(4, event(240)))], ack_delay=2.)
    assert observed == [(10., 240.)]
    assert port.sample is None


def test_reader_preserves_first_fragment_time():
    """A valid fragmented observation keeps its original reception timestamp."""
    wire = packet(4, event(240))
    observed, port = run_scripted_serial([(10., wire[:10]), (10.4, wire[10:])])
    assert observed == [(10., 240.)]
    assert port.sample == (10., pytest.approx(math.pi / 2))


@pytest.mark.parametrize('split', [0, 10])
@pytest.mark.parametrize('retry', [False, True])
def test_reader_disconnects_after_a_long_read_gap(split, retry, direction_logs):
    """Unknown-age bytes and their later retries cannot turn into observations."""
    wire = packet(4, event(240), b'\x11\0')
    following = wire if retry else packet(4, event(60), b'\x12\0')
    chunks = [(10., wire[:split])] if split else []
    observed, port = run_scripted_serial(chunks + [(12., wire[split:]), (12.1, following)])
    assert observed == []
    assert port.closed and not port.connected
    assert port.sample is None
    assert port.writes == []
    assert port.chunks == [(12.1, following)]
    assert len(direction_logs.records) == 1
    assert 'restart required' in direction_logs.records[0].message


def test_delayed_reader_start_cannot_retimestamp_startup_backlog(monkeypatch, direction_logs):
    """Time since input reset counts even before the reader thread first runs."""
    import sys
    from types import SimpleNamespace
    from malbut_system_manager import sound_direction

    clock = [10.]
    wire = packet(4, event(240), b'\x11\0')

    class Port:
        in_waiting = len(wire)
        closed = False

        def open(self):
            pass

        def reset_input_buffer(self):
            self.reset_at = clock[0]

        def read(self, count):
            # The event accumulated after reset while the reader was unscheduled.
            return wire[:count]

        def write(self, data):
            pytest.fail('A startup backlog must not be processed as a fresh frame')

        def close(self):
            self.closed = True

    class DelayedThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            clock[0] = 12.1
            self.target()

        def join(self, timeout):
            pass

    port = Port()
    monkeypatch.setitem(sys.modules, 'serial', SimpleNamespace(Serial=lambda *a, **kw: port))
    monkeypatch.setattr(sound_direction, 'Thread', DelayedThread)
    reader = sound_direction.SerialDirections('/dev/test', clock=lambda: clock[0])
    assert port.reset_at == 10.
    assert port.closed and not reader.connected
    assert reader.take(12.2) is None
    assert not reader.samples
    assert len(direction_logs.records) == 1
    assert 'restart required' in direction_logs.records[0].message


def test_regular_empty_reads_are_not_a_reader_stall():
    """A quiet microphone can emit a fresh wake after many ordinary timeouts."""
    chunks = [(10. + step / 5., b'') for step in range(16)]
    observed, port = run_scripted_serial(chunks + [(13.2, packet(4, event(240)))])
    assert observed == [(13., 240.)]
    assert port.resets == 0
    assert port.sample == (13., pytest.approx(math.pi / 2))


def test_partial_serial_reads_acknowledge_without_changing_device(monkeypatch):
    """Partial serial reads acknowledge without changing device."""
    import sys
    import threading
    from types import SimpleNamespace
    from malbut_system_manager.sound_direction import SerialDirections

    class Port:
        def __init__(self, *args, **kwargs):
            self.data = bytearray(packet(1, b'\xa5\0\0\0')
                                  + packet(4, event(240, nested=True), b'\x02\0')
                                  + packet(4, b'{"type":"version"}', b'\x03\0')
                                  + packet(4, event(240, nested=True), b'\x02\0'))
            self.writes = []
            self.drained = threading.Event()
            self.stopped = threading.Event()
            self.closed = False

        def open(self):
            pass

        def reset_input_buffer(self):
            pass

        @property
        def in_waiting(self):
            return min(5, len(self.data))

        def read(self, count):
            if not self.data:
                # Next read means all preceding frames have been processed.
                self.drained.set()
                self.stopped.wait(.01)
                return b''
            data = bytes(self.data[:count])
            del self.data[:count]
            return data

        def write(self, data):
            self.writes.append(data)

        def close(self):
            self.closed = True
            self.stopped.set()

    port = Port()
    monkeypatch.setitem(sys.modules, 'serial', SimpleNamespace(Serial=lambda *a, **kw: port))
    reader = SerialDirections('/dev/test', clock=lambda: 10.)
    try:
        assert port.drained.wait(2)
        # The duplicate arrived after another event; it still belongs to the
        # original wake and must not become a second direction measurement.
        assert reader.take(10.5) == (10., pytest.approx(math.pi / 2))
        assert reader.take(10.5) is None
        reader.close()
        assert port.writes == [packet(0xff, b'\xa5\0\0\0', bytes([identity, 0]))
                               for identity in (0, 2, 3, 2)]
        assert not reader.connected and port.closed
        assert reader.take(10.5) is None
    finally:
        reader.close()
