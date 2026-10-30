"""Check fresh microphone frames in an expendable process before loading models."""

import sys

from malbut_stt.audio import SoundDeviceRecorder


def main():
    recorder = None
    try:
        recorder = SoundDeviceRecorder(device_index=int(sys.argv[1]))
        recorder.start()
        # A successful open alone cannot detect a stalled USB/PulseAudio route.
        for _ in range(10):
            recorder.read()
        return 0
    except Exception as error:
        print('speech_microphone_probe_failed: ' + type(error).__name__, flush=True)
        return 1
    finally:
        if recorder is not None:
            try:
                recorder.stop()
            finally:
                recorder.delete()


if __name__ == '__main__':
    raise SystemExit(main())
