# Local whisper.cpp bridge (ABI 2)

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
latency, and device memory use. Confirm ABI 2 loading and inference before adding
TTS and YOLO. A Mac CPU build or a successful link does not establish Jetson CUDA
compatibility, simultaneous-workload capacity, or real microphone performance.

For a build-only host check, use a separate directory and replace the CUDA flags
with `-DGGML_CUDA=OFF -DGGML_METAL=OFF`; omit the CUDA architecture flag. This can
verify the bridge ABI and library resolution without loading a model or using a
GPU. Stop and join the ASR worker before closing its resident transcriber.

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

`max_utterance_s=0.0` disables the duration limit at the ROS boundary; a positive
value preserves an explicitly requested limit. The default fallback silence is
2 seconds, with endpoint inference starting at 0.8 seconds and completed
sentences eligible to finish after 1 second. A custom fallback of 1 second or
less disables predecode and retains that shorter fallback behavior. Partial
transcription continues at its existing 2-second interval.

Choose `device_index` from the robot's PvRecorder device list. The Mac device
index does not identify a Jetson microphone. `input_has_aec=false` remains the
default; set it to true only for audio that already passed through a verified
echo cancellation path. This profile does not install or enable AEC.
