"""Check the Jetson STT runtime without publishing or retaining microphone audio."""

import struct


def check_stt(model_path, library_path, *, device_index=0, cpp_threads=6):
    """
    Load the local ABI 3 model and read one 16 kHz microphone frame.

    This requests GPU use but does not prove CUDA execution or transcription
    quality. The caller must bound the process lifetime for stalled drivers.
    """
    if type(device_index) is not int or device_index < -1:
        raise ValueError('device_index must be -1 or a microphone index')
    from malbut_stt.audio import SoundDeviceRecorder
    import webrtcvad

    from malbut_stt.cpp_transcription import CppWhisperTranscriber

    vad = webrtcvad.Vad(2)
    with CppWhisperTranscriber(
        model_path, library_path, use_gpu=True, n_threads=cpp_threads,
    ) as transcriber:
        recorder = SoundDeviceRecorder(
            frame_length=512,
            device_index=device_index,
        )
        started = False
        try:
            if recorder.sample_rate != 16000:
                raise ValueError('STT microphone must provide 16 kHz PCM')
            recorder.start()
            started = True
            samples = recorder.read()
            if len(samples) != 512:
                raise ValueError('STT microphone returned an incomplete frame')
            # Match the node's 20 ms WebRTC VAD input; do not transcribe or log it.
            vad.is_speech(struct.pack('<320h', *samples[:320]), 16000)
        finally:
            try:
                if started:
                    recorder.stop()
            finally:
                recorder.delete()
        return {
            'bridge_abi': transcriber.metadata['bridge_abi'],
            'model_type': transcriber.metadata['model_type'],
            'requested_use_gpu': True,
            'cuda_execution_verified': False,
            'microphone_sample_rate': 16000,
        }
