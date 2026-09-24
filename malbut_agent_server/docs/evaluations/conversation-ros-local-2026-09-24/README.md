# Local ROS communication validation — 2026-09-24

Current source was exercised against real ROS 2 Humble middleware and newly built
`malbut_interfaces` in local ARM64 Docker snapshots. Tests use fixed providers,
synthetic transcript/PCM/image inputs and a text-receipt-only TTS node.

Final: **36 passed, 0 failed, 0 skipped in 9.82 s**. Host unit regressions:
**57 passed** against primary and **57 passed** against mirrored runtime imports.
The macOS full suite may still report ROS import skips; this separate Linux run
executes their previously skipped test bodies.

- `results.json`: initial failures, classifications, final per-test results and cleanup.
- `environment.json`: initial environment and verified container isolation.
- `pytest.log` / `pytest.xml`: first run, 28 passed and 8 failed.
- `post-fix/`: cancellation fix and test-contract updates, 35 passed and one remaining
  location-cancellation test awaiting an obsolete reply.
- `final/`: corrected location-cancellation expectation and all 36 tests passing.
- `run-host.sh` and `run-in-container.sh` in each run: exact executable commands.

The first run reused a local snapshot of stopped `malbut-speech-retry-test`.
The final run reused stopped `malbut-rosorin-sim-20260918`, which already included
`cv_bridge`. `docker commit` created local snapshots only: no image download or
package installation occurred. Existing containers were never started or modified.
Current source was mounted read-only; build/install output used container `/tmp`.
All test containers used `--network none`, no devices, no published ports, dropped
capabilities, a read-only root filesystem and `ROS_LOCALHOST_ONLY=1`. The outer ROS
domain was 199; isolated fixtures used 193 and 197. No external DDS or API was possible.

The runtime correction propagates a canceled Manager mission as `CancelledError`
and ends the speech request without publishing late progress/error/answer text,
while releasing capacity for the next request. ROS assertions separately verify
mission terminal state, speech progress and final replies, one retry at most,
no duplicate re-execution, no late location write and memory deletion redaction
after resuming a persisted conversation.

All three temporary test containers and both temporary snapshot images were
removed after collecting results. Existing containers remain stopped. Docker
Desktop was started by this task and remains running in the background.

To reproduce, recreate the relevant local snapshot using the `docker commit`
command documented in the selected `run-host.sh`, then run that script. These
scripts intentionally never pull images or install packages. This is local ROS
communication evidence, not physical robot, microphone, speaker or model-quality
validation.
