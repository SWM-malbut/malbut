# Malbut Voice M0

`malbut_voice` is a deliberately narrow, local-only source for one final
transcript. It requires an explicit operator `--microphone` action, captures a
fixed ALSA hardware device into bounded memory, and runs an already-provisioned
faster-whisper snapshot without network lookup.

It does not implement wake-word detection, continuous listening, ROS or HTTP
publishing, TTS, speaker recognition, or Tool execution authority. A bare
`SpeechTranscriptEvent` remains untrusted. Only the instance-private
`VerifiedMicrophoneFinal` wrapper can carry the package's physical microphone
provenance claim, and its public audit intentionally excludes transcript text,
speaker labels, PCM hashes, model paths, and private receipts.

The installed JSON file is an example for this host, not an active
configuration. Provision the model and manifest beneath protected directories,
copy the configuration to an absolute protected path, correct its immutable
bindings, and remove group/world write permissions. `--check` performs static
device, executable, ALSA configuration, model, and runtime verification without
starting `arecord`.

The service supervisor must set all three offline controls to the literal value
`1` before either CLI action: `HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE`, and
`HF_HUB_DISABLE_TELEMETRY`. The package fails closed instead of changing the
process-global environment. The fixed capture command remains
`/usr/bin/arecord`; on the audited host that entry must be the protected relative
link `arecord -> aplay`, while `/usr/bin/aplay` is the root-owned hashed ELF that
must match both the configuration and the live child `/proc/<pid>/exe` inode.

The deployment owner is part of this trust boundary. Production configuration,
manifest, package code, model root, snapshot directories, and blob directories
should be root-owned and supplied from a read-only mount; fs-verity or an
equivalent immutable store is preferred. The implementation also permits the
current effective UID so a locked-down service account and deterministic tests
can operate, and it re-verifies all model hashes immediately after the initial
model load. It does not claim resistance to a malicious same-UID process that
can swap a model and restore it between those checks. Such a deployment must not
describe the resulting transcript as independently provenance-secured.

```bash
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export HF_HUB_DISABLE_TELEMETRY=1
ros2 run malbut_voice malbut-microphone-stt \
  --config /etc/malbut/microphone-stt.json --check
ros2 run malbut_voice malbut-microphone-stt \
  --config /etc/malbut/microphone-stt.json --microphone
```

The second command captures only because `--microphone` is present. It prints a
content-free audit by default. Add `--show-transcript` only when exposing the
recognized text to the current terminal is intentional.

## Validation

The ordinary package suite never opens `arecord`. The sole hardware test is
skipped unless an operator provides both a protected active configuration and
the exact `MALBUT_RUN_MIC_SMOKE=I_UNDERSTAND_THIS_OPENS_THE_MICROPHONE`
opt-in. The current no-capture regression result is `85 passed, 3 skipped`;
the other two skips are ament lint wrappers unavailable in this local Python
environment, while direct `flake8` and `pydocstyle` checks pass.

```bash
cd ~/ros2_ws/src/malbut/malbut_voice
PYTHONPATH=.:../malbut_agent_server python3 -m pytest -q test
python3 -m flake8 malbut_voice test
pydocstyle malbut_voice test
```
