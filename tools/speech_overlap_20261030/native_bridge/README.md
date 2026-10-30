# Frozen bridge four-window native comparison v1

This is a self-contained pure NumPy computation pack. It does not access ROS,
models, PulseAudio, ADB, audio devices, the normal service, or the network.
Only the parent may stage/run it. The normal quiet-duck-only service remains
unchanged; no physical test should overlap this compute diagnostic.

`original.py`, `proposed.py`, `startup_phase.py`, `weights.npy`, and the entire
15,360,000-byte `raw8.pcm` are copied byte-for-byte from their pinned sources.
The original 8-file Mac comparison remains immutable. `protocol.json` records
source hashes and the saved raw tail/phase. `sha256.json` pins every pack file;
the parent must pin that manifest itself before staging, then verify each entry
again remotely. The runner verifies the manifest and payload before imports
and again before/after each measured child and at completion.

Native execution requires Linux arm64, the existing NumPy 1.24, assertions on,
and OPENBLAS/OMP/VECLIB thread variables each set to 1. No package install is
needed. Output must be a new directory. For example, after verified staging:

```text
systemd-run --user --wait --pipe --collect \
  --unit=malbut-bridge-batch-native-v1 \
  --property=MemoryMax=350000000 --property=MemorySwapMax=0 \
  --property=RuntimeMaxSec=120 --property=TimeoutStopSec=5 \
  --property=KillMode=control-group \
  --setenv=OPENBLAS_NUM_THREADS=1 --setenv=OMP_NUM_THREADS=1 \
  --setenv=VECLIB_MAXIMUM_THREADS=1 \
  /usr/bin/python3 PACK/run.py NEW_OUTPUT_DIRECTORY
```

Replace only PACK and NEW_OUTPUT_DIRECTORY with the parent's staged paths.
This command is documentation; preparation did not run it. The cgroup limit
is the enforced resource boundary, not a prediction from Mac RSS. RuntimeMax
is an external bound; six individually bounded 30-second children are not a
guarantee the complete experiment fits inside 120 seconds. A timeout, kill,
missing output, or `complete: false` is incomplete and cannot be graded as a
full pass. The normal voice service is in a different cgroup.

Before any timing, the pack compares all 2,323 bridge blocks (including the
entire 60-second raw tail), 24 edge blocks and 6 invalid shapes. It requires
between-arm float/PCM and boundary-state equality on the native host. There
is no requirement that native hashes match Mac hashes: FFT implementations
and NumPy versions differ. Both arms start in the same fresh state because
the raw tail omits previous historical state. Phase warmup uses unchanged
128-sample processing; no socket/thread constructor runs.

Only after all parity checks pass, six fresh Python processes run in the
fixed A/B, B/A, A/B order. Timed CPU/wall includes raw conversion, phase
prepare/written, filter, stereo pack and tobytes; excludes imports, hashing,
construction and tracing. Linux ru_maxrss is preserved in KiB and converted
to bytes with ×1024. A separate one-block tracemalloc measurement is excluded
from CPU/wall. Neither value is whole-stack memory or a hard memory proof.

The atomic result begins as incomplete, records completed timing rows, and
becomes complete only after all nine parity cases, all six correct children,
matching last-output hashes, and final source verification. A stage/error
remains on controlled failure; abrupt external termination can leave only
the last incomplete checkpoint. Partial parity is not reported as a full
phase. Capture stdout/stderr as well as results and unit outcome before
fetch/hash verification. This is not the idle whole-voice CPU acceptance test.

Local fake tests (no full replay or native timing):

```text
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q PACK/test_pack.py
```
