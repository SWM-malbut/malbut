# Local whisper.cpp bridge (ABI 3)

This builds the resident STT bridge against an explicitly supplied local
[whisper.cpp checkout](https://github.com/ggml-org/whisper.cpp/tree/da54572229bcf64ba367d96c7ef15770376c4280).
Configuration requires that checkout to be clean at commit
`da54572229bcf64ba367d96c7ef15770376c4280`. Nothing is downloaded or installed
system-wide by these commands. Keep the external checkout and build directory
outside each other so generated files do not dirty the pinned source.

Prepare a working C++ toolchain, Git, CMake >= 3.18, and the CUDA toolkit matching
the robot's JetPack installation before building. The example below targets
Jetson Orin NX, whose [CUDA compute capability is 8.7](https://developer.nvidia.com/cuda/gpus).
It is a preparation recipe; successful execution on the target robot is still
required.

From the Malbut repository root, replace the local source path:

```bash
cmake -S malbut_stt/native -B .runtime/stt-cuda-build \
  -DWHISPER_CPP_SOURCE_DIR=/absolute/path/to/whisper.cpp \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_CUDA=ON -DGGML_METAL=OFF \
  -DCMAKE_CUDA_ARCHITECTURES=87
cmake --build .runtime/stt-cuda-build --target malbut_whisper --parallel 2
```

The Linux bridge is `.runtime/stt-cuda-build/bin/libmalbut_whisper.so`.
Pass its **absolute path** as `library_path` to `CppWhisperTranscriber`.
Its shared whisper/ggml dependencies are in the same build's `bin` directory;
CMake records build-tree library search paths. Keep that build tree intact.
Copying the bridge alone or using the Mac `.dylib` does not provide a Linux
runtime. Model files are independent of this build.

Rebuild the bridge when updating from ABI 2. The Python adapter rejects older
libraries before model creation because ABI 3 adds cancellation and a decode
deadline to the native call.

Use the already acquired multilingual `ggml-small.bin` from the
[official model location](https://huggingface.co/ggerganov/whisper.cpp/blob/main/ggml-small.bin):

- Size: `487601967` bytes; GGML mostly F16.
- SHA-256: `1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b`.
- Verify the local file with `sha256sum /absolute/path/to/ggml-small.bin` before
  passing that path as `model_path`. No credentials or model download are needed
  at runtime.

`GGML_CUDA=ON` and `use_gpu=True` express intent, not proof of GPU execution.
On the robot, check the model initialization log for the selected CUDA device
and CUDA backend, then run a fixed local PCM fixture and record its transcript,
latency, and device memory use. Confirm ABI 3 loading and inference before adding
TTS and YOLO. A Mac CPU build or a successful link does not establish Jetson CUDA
compatibility, simultaneous-workload capacity, or real microphone performance.

For a build-only host check, use a separate directory and replace the CUDA flags
with `-DGGML_CUDA=OFF -DGGML_METAL=OFF`; omit the CUDA architecture flag. This can
verify the bridge ABI and library resolution without loading a model or using a
GPU. Signal `transcriber.cancel()` before joining the ASR worker. `close()` also
signals cancellation and waits for the native call to return before freeing its
context; repeated close is safe.

Each native decode has a finite `decode_timeout_s` (default: 30 seconds), exposed
as the ROS parameter `stt_decode_timeout_s`. Cancellation raises `RuntimeError`
and timeout raises `TimeoutError`; neither returns partial text. A later decode
can reuse the context. Whisper's abort and encoder callbacks enforce this
cooperatively: a GPU kernel or driver that never returns cannot be interrupted
by this deadline and still requires the process supervisor's shutdown boundary.

## ROS node

Use a Python environment compatible with the installed ROS distribution.
`requirements-whisper-cpp.txt` lists only the PCM, microphone and VAD packages;
this backend does not require CTranslate2 or an OpenAI transcription client.
After building `malbut_interfaces` and `malbut_stt` with colcon and sourcing the
workspace, supply the actual local model and library paths:

```bash
ros2 run malbut_stt stt --ros-args \
  --params-file "$(ros2 pkg prefix malbut_stt)/share/malbut_stt/config/jetson.yaml" \
  -p stt_model_path:=/absolute/path/to/ggml-small.bin \
  -p stt_library_path:=/absolute/path/to/stt-cuda-build/bin/libmalbut_whisper.so
```

The profile selects `backend=whisper_cpp`, requests GPU execution, and uses six
CPU helper threads. Wake recognition and ordinary/partial transcription share
one model; leave `wake_model_path` empty or point it at that same model file.
The default backend without this profile remains `faster_whisper` on CPU.

`max_utterance_s=0.0` leaves total utterance duration unlimited. The
`max_buffer_s=60.0` limit applies only to retained PCM: successful incremental
results acknowledge stable segment boundaries, release older audio, and keep
the full accumulated text until one final publication at the natural endpoint.
Repeated words are preserved using timed overlap rather than text deduplication.
If inference stalls or no stable boundary can be established before unprocessed
audio fills the buffer, capture reports `utterance_discarded:buffer_overflow`;
it never sends an incomplete prefix as a successful command. The default
fallback silence is 2 seconds, with endpoint inference starting at 0.8 seconds and completed
sentences eligible to finish after 1 second. A custom fallback of 1 second or
less disables predecode and retains that shorter fallback behavior. Partial
transcription continues at its existing 2-second interval.

Choose `device_index` from `python -m sounddevice` in the robot speech runtime.
The default is `0`: the current robot's `XFM-DP-V0.0.18: USB Audio`, ALSA
`hw:0,0`. IDs are PortAudio device indices, not an input-only list position.
An explicit `-1` selects the system default, which was a different Jetson input
in the hardware check. Release the XFM from the manufacturer
`xf_mic_asr_offline/voice_control` process and disable its entry in the external
`startup_check` before use; see the [speech guide](../../malbut_bringup/README_SPEECH.md).
Recheck device IDs when the hardware changes. `input_has_aec=false` remains the
default; set it to true only for audio that already passed through a verified
echo cancellation path. This profile does not install or enable AEC.
