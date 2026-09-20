"""Play short local speech acknowledgements without synthesis requests."""

from array import array
from math import pi, sin


def play_wake_chime(device_index=-1):
    """Return after the 180 ms PCM tone has drained to the selected speaker."""
    _play_chime((880, 1174), 0.09, 3900, device_index)


def play_endpoint_chime(device_index=-1):
    """Acknowledge the accepted speech endpoint with a softer 150 ms tone."""
    _play_chime((660,), 0.15, 2600, device_index)


def _play_chime(frequencies, note_s, amplitude, device_index):
    import sounddevice as sd

    sample_rate = 24000
    samples = array('h')
    for frequency in frequencies:
        count = int(sample_rate * note_s)
        for index in range(count):
            # Short fades prevent clicks at the start/end of each note.
            envelope = min(1.0, index / 240, (count - 1 - index) / 240)
            samples.append(round(amplitude * envelope * sin(2 * pi * frequency * index / sample_rate)))
    stream = sd.RawOutputStream(
        device=None if device_index == -1 else device_index,
        channels=1, dtype='int16', samplerate=sample_rate,
    )
    try:
        stream.start()
        stream.write(samples.tobytes())
        stream.stop(ignore_errors=False)  # Wait for device drain before return.
    finally:
        stream.close(ignore_errors=False)
