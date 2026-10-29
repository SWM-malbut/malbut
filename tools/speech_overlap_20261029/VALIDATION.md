# Publication validation — 2026-10-29

The PR targets `wip/local-code-handoff-20261028`. Validation below was run locally
before publishing this checkpoint; it is not a ROS build or a new robot trial.

|Scope|Result|
|---|---:|
|Repository STT reply-boundary, Reachy barge-in and dialogue pipeline tests|178 passed|
|Repository TTS node and key-notice tests|49 passed|
|Reconstructed runtime19: candidate evidence, fake output startup, phase estimator|56 passed|
|Source reconstruction|135 hashes match:15 archived overlays+120 repository files|
|First-trial result count|7 pass+1 partial+3 fail=11; retests/follow-ups separate|

From the repository root, with `pytest` and `numpy` installed:

```sh
PYTHONPATH=malbut_stt:malbut_tts python3 -m pytest -q \
  malbut_stt/test/test_reply_boundary.py \
  malbut_stt/test/test_reachy_barge_in.py \
  malbut_stt/test/test_dialogue_pipeline.py
PYTHONPATH=malbut_tts python3 -m pytest -q \
  malbut_tts/test/test_node.py malbut_tts/test/test_key_notice.py
python3 tools/speech_overlap_20261029/check_checkpoint.py --test
```

The56 candidate checks run against the isolated archived runtime, while the227
repository checks run against the canonical source. Neither is the complete legacy
suite or proof of acoustic performance. A full device transcript, latency, CPU and
recovery acceptance remains open.

Raw audio, databases, model/ADB binaries and credential files are excluded from
this publication. The new text bundle was checked for private-key/API-token patterns
and personal workstation/network paths; no matches were found. Frozen runtime
source retains the generic robot installation/device paths it actually used.
