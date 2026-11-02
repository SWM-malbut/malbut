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


@pytest.mark.parametrize('angle', [None, True, '90', -1, 360, math.inf, math.nan])
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
        assert len(port.writes) == 4
        assert all(frame[2] == 0xff for frame in port.writes)
        assert not reader.connected and port.closed
        assert reader.take(10.5) is None
    finally:
        reader.close()
