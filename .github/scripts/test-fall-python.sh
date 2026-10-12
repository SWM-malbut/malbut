#!/usr/bin/env bash
# Offline only: no ROS, model downloads, credentials or provider requests.
set -euo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
exec python "$repo_root/.github/scripts/run-python-tests.py" fall "$@"
