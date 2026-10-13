# Silero admission, VAD observation and offline comparison

## Block non-speech before Whisper (optional)

The admission gate checks the actual PCM window immediately before command
Whisper model calls: incremental previews, endpoint checks, final decoding and
incremental recovery. Wake recognition bypasses this gate and retains its
existing WebRTC/Whisper path, including when it shares the command model's
underlying Whisper instance. It uses the pinned Silero model below,
CPU execution with one inference thread, probability >=0.5 and at least 80ms of
consecutive positive input. At the 32ms frame grid this normally needs three
positive frames (96ms). Padding never counts toward the required duration.

Install `requirements-shadow-vad.txt` into the STT Python environment and
provision the verified ONNX model. Add this before the existing launch command:

```bash
export MALBUT_STT_SILERO_GATE_MODEL='/absolute/path/to/silero-v6.2.3.onnx'
exec ros2 launch malbut_bringup cloud.launch.py
```

This is separate from `MALBUT_STT_SHADOW_VAD_MODEL`: that variable still enables
observation only. The gate works without the observer or incident recorder.
Enabling both loads two Silero sessions. Unset `MALBUT_STT_SILERO_GATE_MODEL`
to restore the existing behavior on the next run. With neither variable set,
ONNX Runtime and model weights are not loaded.

The gate does not remove individual frames, join separated speech, change
WebRTC capture/endpoint rules, extend the chime tail, or filter particular words.
Accepted input is sent to Whisper unchanged. Each model input resets Silero's
state, so earlier speech cannot admit a later silent cropped input. Once the
consecutive-speech condition is met, analysis stops; the logged peak covers
only `scanned_samples`, not necessarily the whole input. Whisper can still
hallucinate on accepted inputs, including noise falsely accepted as speech.

An explicit missing/wrong model or missing runtime fails STT startup before
Whisper/microphone initialization. Runtime gate failures and cancellation do
not bypass the gate. Rejection raises `SileroNonSpeech` through the existing
empty/failed-result path: no final transcript or preview is published, normal
failed turns end with the existing failure cue, and Agent-owned sessions retain
their existing failed-input policy. Preview/final retries are each gated again.

`silero_gate` logs include the decision, threshold, examined duration, longest
speech run, peak score and exact PCM hash. `silero_gate_error` and
`silero_gate_cancelled` explain unavailable/cancelled decisions. Rejected inputs
have no Whisper `decode_input` or `decode_result`. Saved incident windows show
these under **STT 실행 추적 → VAD**, distinguished from observation-only rows.
Incident text triggers are unchanged: a blocked input creates no text to trigger
an incident. These events are saved only when another configured trigger retains
their window; no bundle does not mean no rejection. The console reports final
rejections as `transcription_failed:SileroNonSpeech`.

The gate is an experiment, not a verified replacement for real microphone
testing. Quiet speech and short `네`/`아니요` may be rejected. Keep the raw input
and labelled expected text when evaluating false rejections, and do not interpret
mocked short-reply tests as measured human speech recall.

## Observe the existing microphone without changing STT

Optional shadow observation uses the existing microphone's 512-sample PCM
chunks, before speaker/busy gates. A separate bounded worker runs Silero
v6.2.3 at 16kHz/32ms and an independent WebRTC mode matching the node's
`vad_mode` at 20ms. Production WebRTC and Whisper behavior remain unchanged:
no result is blocked, no endpoint is changed, and no extra microphone opens.
The shadow WebRTC has continuous raw-input state, so its decisions can differ
from production WebRTC after gating; production decisions remain `vad_frame`.

Install `requirements-shadow-vad.txt` into the Python environment running the
STT node, and provision the official ONNX file specified below. The worker
verifies its SHA-256; it never downloads weights. Before the existing
`exec ros2 launch malbut_bringup cloud.launch.py`, add:

```bash
export MALBUT_STT_SHADOW_VAD_MODEL='/absolute/path/to/silero-v6.2.3.onnx'
```

Remove/unset this variable to disable observation on the next run. The
ordinary default is disabled. A missing model/runtime disables only shadow
observation and logs `stt_shadow_vad_stopped:<error type>`.

The normal incident recorder stores scores in its existing `events.jsonl`;
its default is `save_audio=false`. Shadow observation does not enable WAV
saving. Do not enable `MALBUT_STT_DIAGNOSTIC_DIR` for this logs-only workflow,
since that existing continuous decoder diagnostic mode saves audio.
If incident capture is explicitly disabled, shadow observation is disabled too.

In the resource viewer's **STT 실행 추적 → VAD** filter, `VAD 관찰 비교`
shows Silero's raw probability (speech at >=0.5), shadow WebRTC frame votes,
input blocking status, observer queue wait and dropped chunk count.
`원본 필드 보기` includes exact sample ranges, PCM hashes, input capture
monotonic time, processing time, model hash and thresholds. A Silero row
covers 512 samples; its WebRTC list contains newly completed 320-sample frames,
which can start in the previous chunk. Do not treat the row's votes as two
decisions on an identical time interval. The displayed row wall time is the
event's processing time; `source.captured_at` is microphone receipt time.

The worker queue holds at most 64 chunks (2.048 seconds / 64KiB PCM). Queue
overflow drops observation copies only. A missing chunk, microphone overflow,
or capture-time gap resets both shadow states and records `shadow_vad_gap`.
Production generation/gate changes alone do not reset the continuous shadow
detectors. `shadow_vad_stopped` reports any unprocessed shutdown tail. The
observer adds CPU load, so check inference time/queue lag on the target robot.

Evidence is saved when the incident recorder's configured text trigger fires,
not continuously. To retain normal short-reply trials too, supply a local JSON
config to `MALBUT_STT_INCIDENT_CONFIG`, preserving any required existing limits
and triggers. For example:

```bash
export MALBUT_STT_INCIDENT_CONFIG='{"save_audio":false,"contains":["구독","시청해주셔서","자막제공"],"exact":["감사합니다","네","아니요","테스트종료"]}'
```

Test silence, chimes, normal `네`/`아니요`, and quiet replies, marking the
actual spoken text/time separately. For a quiet trial with no recognized
trigger, say `테스트 종료` within 30 seconds to save the preceding window
(the phrase must be recognized as a final command). Wait at least 15 seconds
after the trigger for the full trailing window. No matching final transcript
means no bundle; absence of a bundle does not prove either VAD worked.
These records measure VAD evidence, not the accuracy of a future replacement
endpoint/segment algorithm. Real microphone short-reply validation is required
before changing production admission.

## Offline tools

These tools read existing files. They do not open ROS, a microphone, a speaker,
or a network connection, and do not change the production VAD or thresholds.

## Inspect actual decoder decisions

Run from the repository root, using an absolute path to downloaded incident
bundles and a new output directory:

```bash
python3 malbut_stt/tools/audit_native_decisions.py \
  --incidents /absolute/path/to/incidents --output /absolute/path/to/audit
```

`audit.json` and `windows.csv` preserve per-window scores, both conditions of
the native silence rule, the actual skip flag, timestamps and source lines.
Overlapping incidents are deduplicated by decode ID; conflicting duplicates
and missing/nonfinite scores are reported rather than treated as silence.
An observed decision that matches the code does not establish that the speech
classification was correct. User confirmation or labelled audio is needed.

## Compare two VADs on identical PCM

Install `requirements-vad-evaluation.txt` into a separate Python environment.
Provide the official Silero v6.2.3 ONNX model locally:

- Source: https://github.com/snakers4/silero-vad/tree/5cd7945676eb32225748052e2e6a0580e4686a08
- File: `src/silero_vad/data/silero_vad.onnx`
- SHA-256: `1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3`
- Runner state/context protocol follows that revision's `OnnxWrapper`.

Use a JSON list of cases. WAV paths are relative to the manifest or absolute;
files must be mono PCM16 at 16kHz, nonempty, and at most 60 seconds long:

```json
[
  {"id": "quiet", "wav": "quiet.wav", "label": "non_speech", "provenance": "room recording, confirmed no voices"},
  {"id": "yes", "wav": "yes.wav", "label": "speech", "expected_text": "네", "provenance": "human reply"},
  {"id": "unknown", "wav": "unknown.wav", "label": "unknown"}
]
```

Optional `pcm_sha256` prevents unnoticed input changes. `initial_prompt` is
null by default; specify the actual wake hint only for a wake decode experiment.
Labels and expected text are supplied by the operator, never inferred from the
STT output. Unknown cases are excluded from false-admission/rejection counts.

```bash
PYTHONPATH=malbut_stt python3 malbut_stt/tools/compare_vad.py \
  --manifest /absolute/path/to/cases.json \
  --silero-model /absolute/path/to/silero_vad.onnx \
  --output /absolute/path/to/new-comparison
```

To also observe the same Whisper small/whisper.cpp decoder, add both
`--whisper-model /absolute/path/to/ggml-small.bin` and
`--whisper-library /absolute/path/to/libmalbut_whisper.so`. ABI 4 includes native
scores; ABI 3 reports them unavailable. This comparison runs Whisper on CPU,
with the existing Korean greedy/no-history settings. Model hashes, actual
metadata, library hash, package versions and full frame decisions are saved.

The default gate asks for 80ms of consecutive positive frames, matching the
existing capture onset duration. WebRTC uses mode 2 and 20ms frames; Silero
uses 32ms frames and raw probability >= 0.5. State resets for each clip. The
last frame is zero padded for inference, but padding never adds voiced time.
The different frame grids mean 80ms requires 4 WebRTC or 3 full Silero frames.
These are explicit experiment settings, not tuned production recommendations.

`results.jsonl` checkpoints each case, `comparison.csv` provides a compact
table, and `summary.json` counts speech rejections and non-speech admissions.
Optional Whisper decodes the full unchanged PCM once per case, even if both
VADs reject it. The two gates only determine whether that same result would be
admitted. This is not Silero's segment-extraction algorithm, streaming endpoint
replay, or a measurement of live robot responses. Decoder errors are recorded
separately, and the process exits nonzero if any decode fails.

Synthetic noise/chimes and public read speech are useful controls but do not
reproduce the robot microphone path. A logs-only incident cannot be replayed
through another VAD: the original PCM is required. Do not claim a robot fix
from those controls or omit short/quiet real replies from a deployment study.
