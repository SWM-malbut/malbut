# Local whisper.cpp bridge (ABI 4)

This builds the resident STT bridge against an explicitly supplied local
[whisper.cpp checkout](https://github.com/ggml-org/whisper.cpp/tree/da54572229bcf64ba367d96c7ef15770376c4280).
Configuration requires that checkout to be clean at commit
`da54572229bcf64ba367d96c7ef15770376c4280`. Nothing is downloaded or installed
system-wide by these commands. Keep the external checkout and build directory
outside each other so generated files do not dirty the pinned source.

Prepare a working C++ toolchain, Git, Python 3, CMake >= 3.18, and the CUDA toolkit matching
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

Rebuild the bridge for ABI 4 native decision tracing. ABI 3 libraries continue
to transcribe, but diagnostics explicitly report `bridge_abi_3` as unavailable.
Older ABIs are rejected before model creation because they lack cancellation
and a decode deadline.

### Native silence decision tracing

The pinned version already contains whisper.cpp PRs [#2625](https://github.com/ggml-org/whisper.cpp/pull/2625)
and [#2629](https://github.com/ggml-org/whisper.cpp/pull/2629). ABI 4 observes their
actual decisions without changing thresholds, tokens, timestamps, or seek logic.
CMake generates `whisper-observed.cpp` in the build directory using
`instrument_whisper.py`; the supplied clean upstream checkout stays untouched.
Reconfiguration fails if the observation point is missing or ambiguous.

When incident or continuous diagnostics are enabled, each decode result includes
`native_decode.windows` (up to 64 windows, with an explicit truncation flag):

- `no_speech_prob`, `avg_logprobs`: the decoder's actual window-level values,
  including windows discarded before producing segments. The score is the
  selected sequence's average, not a reconstruction from emitted text tokens.
- `no_speech_threshold`, `logprob_threshold`, `skipped_no_speech`: the thresholds
  and actual combined decision (`prob > threshold AND score < threshold`).
- `start_s`, `end_s`, `next_seek_s`, `single_timestamp_ending`: input-relative
  window positions and the #2629 decision to advance over the remaining window.
- `segment_start`, `segment_count`, `token_count`: correspondence to the returned
  segments and number of tokens used to compute the selected sequence score.

Each record has a native `return_code`; timeout/cancellation can include partial
observations but never publish partial transcripts. Nonfinite scores are `null`
with their original representation in `nonfinite_fields`. Exact zero input is
reported as `not_run/exact_silence`, not as a measured no-speech probability.
The recorder preserves these fields even with `save_audio=false`. The resource
viewer's STT execution trace shows the scores and decisions per decode.

Python Whisper PR [#1838](https://github.com/openai/whisper/pull/1838) is not ported:
its heuristic requires word alignment and has reported speech-omission cases.
This bridge does not enable word timestamps or add a new silence-skipping rule.

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

## Same-audio performance replay

Run an existing mono 16 kHz PCM16 WAV through the actual VAD, incremental decode,
and endpoint pipeline, without opening a microphone, speaker, ROS, or API:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=malbut_stt python malbut_stt/tools/replay_local.py \
  --backend whisper_cpp \
  --model-path /absolute/path/to/ggml-small.bin \
  --library-path /absolute/path/to/stt-cuda-build/bin/libmalbut_whisper.so \
  --wav /absolute/path/to/korean.wav \
  --output .runtime/replay-F16.json \
  --max-runtime-s 120 2>.runtime/replay-F16.native.log
```

The replay matches the Jetson profile's 0.8-second predecode and 2-second
fallback. It keeps supplying paced silence after EOF while pending inference
finishes. Reports separate model loading, individual decode calls, and the
last VAD speech frame to final-text interval; the latter is not physical
microphone speech-end truth. Preserve the native log to verify the selected
CUDA device. Add `--wake` only for a fixture containing the wake phrase.

For Q8/Q5 comparison, use the same frozen WAV and change only the model/output
paths. Native precision comes from the model file, not `--compute-type`.
CLI runs are cold loads; for warm sequential repetitions, pass one resident
`CppWhisperTranscriber` to `replay_wav(..., transcriber=stt)` and close it after
clean replay shutdown. Do not run a second product STT process during this
baseline. Record whole-device RAM, swap, temperature and YOLO load separately
on Jetson. The [2026-09-26 evaluation](../docs/jetson_8gb_performance_2026-09-26.md)
records candidate selection and current limits.

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
