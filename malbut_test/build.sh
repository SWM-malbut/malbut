#!/usr/bin/env bash
set -euo pipefail

# Run with bash even when the robot's interactive shell is Zsh.
robot_source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
parent_dir="$(dirname -- "$robot_source_dir")"
if [[ "$(basename -- "$parent_dir")" == src ]]; then
  source_dir="$parent_dir"
elif [[ "$(basename -- "$(dirname -- "$parent_dir")")" == src ]]; then
  source_dir="$(dirname -- "$parent_dir")"
else
  echo 'Use <workspace>/src/malbut or <workspace>/src/malbut/malbut_test.' >&2
  exit 1
fi
workspace_dir="$(dirname -- "$source_dir")"
if [[ "${ROS_DISTRO:-}" != humble ]]; then
  echo 'Source ROS 2 Humble and the manufacturer workspace first.' >&2
  exit 1
fi

# Explicit roots bypass only this copy's COLCON_IGNORE, without discovering
# the original/simulation packages or rebuilding manufacturer source packages.
package_paths=(
  "$robot_source_dir/malbut_bringup"
  "$robot_source_dir/malbut_interfaces"
  "$robot_source_dir/malbut_system_manager"
  "$robot_source_dir/malbut_agent_server"
  "$robot_source_dir/malbut_stt"
  "$robot_source_dir/malbut_tts"
  "$robot_source_dir/malbut_yolo"
  "$robot_source_dir/malbut_reid"
  "$robot_source_dir/malbut_tracking"
  "$robot_source_dir/malbut_patrol"
  "$robot_source_dir/malbut_autoslam"
  "$robot_source_dir/malbut_yolo/vendor/yolo_ros/yolo_ros"
  "$robot_source_dir/malbut_yolo/vendor/yolo_ros/yolo_msgs"
)
for package_path in "${package_paths[@]}"; do
  if [[ ! -f "$package_path/package.xml" ]]; then
    echo "Missing bundled package: $package_path (check the Malbut source copy)." >&2
    exit 1
  fi
done
# Check and prepare speech before the expensive cloud/ROS build.
case "${MALBUT_BUILD_SPEECH:-1}" in
  0) ;;
  1)
    speech_cache="${XDG_CACHE_HOME:-$HOME/.cache}/malbut_speech"
    speech_runtime="${MALBUT_SPEECH_RUNTIME:-$speech_cache/runtime}"
    whisper_source="${WHISPER_CPP_SOURCE_DIR:-$speech_cache/whisper.cpp}"
    whisper_build="${MALBUT_STT_BUILD_DIR:-$speech_cache/whisper-cpp-build}"
    speech_model="${MALBUT_STT_MODEL_PATH:-$speech_cache/models/ggml-small.bin}"
    /usr/bin/python3 -c '
import sys
if sys.version_info[:2] != (3, 10):
    raise SystemExit("Robot speech requires the ROS Humble system Python 3.10 (/usr/bin/python3).")
'
    if ! command -v nvcc >/dev/null && [[ -x /usr/local/cuda/bin/nvcc ]]; then
      export PATH="/usr/local/cuda/bin:$PATH"
    fi
    for prerequisite in cmake git nvcc; do
      if ! command -v "$prerequisite" >/dev/null; then
        echo "Missing $prerequisite for robot CUDA speech build. Run: bash \"$robot_source_dir/setup.sh\"" >&2
        exit 1
      fi
    done
    if [[ ! -f "$whisper_source/include/whisper.h" ]]; then
      echo "Missing pinned whisper.cpp checkout: $whisper_source (no automatic download)." >&2
      echo "Run: bash \"$robot_source_dir/setup.sh\"" >&2
      exit 1
    fi
    if [[ ! -s "$speech_model" ]]; then
      echo "Missing STT model: $speech_model (no automatic download)." >&2
      echo "Run: bash \"$robot_source_dir/setup.sh\"" >&2
      exit 1
    fi
    /usr/bin/python3 -c '
import pathlib, sys
source, build, runtime = [pathlib.Path(path).resolve() for path in sys.argv[1:]]
if source == build or source in build.parents or build in source.parents:
    raise SystemExit("Keep the native build outside and separate from the whisper.cpp checkout.")
if runtime == source or source in runtime.parents:
    raise SystemExit("Keep the speech runtime outside the whisper.cpp checkout.")
' "$whisper_source" "$whisper_build" "$speech_runtime"
    # Configure first: the bundled recipe checks the supplied checkout's pin and
    # CUDA toolchain before installing any Python dependencies.
    cmake -S "$robot_source_dir/malbut_stt/native" -B "$whisper_build" \
      "-DWHISPER_CPP_SOURCE_DIR=$whisper_source" \
      -DGGML_CUDA=ON -DGGML_METAL=OFF -DCMAKE_CUDA_ARCHITECTURES=87 \
      -DCMAKE_BUILD_TYPE=Release
    cmake --build "$whisper_build" --target malbut_whisper --parallel "$(nproc)"
    speech_python="$speech_runtime/bin/python"
    if [[ ! -e "$speech_python" ]]; then
      /usr/bin/python3 -m venv --system-site-packages "$speech_runtime"
    fi
    # Invoke the venv path directly; resolving its executable symlink would
    # discard the environment and risk installing into the ROS Python runtime.
    "$speech_python" -c '
import pathlib, sys
venv = pathlib.Path(sys.argv[1]).resolve()
if sys.version_info[:2] != (3, 10):
    raise SystemExit("Speech venv must use Python 3.10.")
if sys.prefix == sys.base_prefix or pathlib.Path(sys.prefix).resolve() != venv:
    raise SystemExit("Speech Python must be the dedicated virtualenv.")
if pathlib.Path(sys._base_executable).resolve() != pathlib.Path("/usr/bin/python3").resolve():
    raise SystemExit("Speech venv must use /usr/bin/python3.")
config = (venv / "pyvenv.cfg").read_text().lower().replace(" ", "")
if "include-system-site-packages=true" not in config.splitlines():
    raise SystemExit("Speech venv must expose ROS system packages.")
' "$speech_runtime"
    "$speech_python" -m pip --isolated install \
      -r "$robot_source_dir/malbut_stt/requirements-whisper-cpp.txt" \
      -r "$robot_source_dir/malbut_tts/requirements-api.txt"
    echo "Built CUDA speech backend: $whisper_build/bin/libmalbut_whisper.so"
    echo "Speech Python: $speech_python"
    ;;
  *)
    echo 'MALBUT_BUILD_SPEECH must be 1 (robot default) or 0 (skip speech setup).' >&2
    exit 1 ;;
esac
# The deployment copy includes cloud media. Its helper prepares/reuses the SDK
# without installing OS packages; setup.sh owns OS dependency installation.
bash "$robot_source_dir/homecam_agent/scripts/build_robot_cloud.sh"
colcon_executable="$(command -v colcon)"
cd -- "$workspace_dir"
# Keep factory build/install hooks intact and avoid upstream's optional uv sync.
PATH=/usr/bin:/bin "$colcon_executable" --log-base log/malbut_test build \
  --base-paths "${package_paths[@]}" \
  --build-base build/malbut_test --install-base install/malbut_test \
  --symlink-install --packages-up-to malbut_bringup "$@"
echo "Built robot copy. In Zsh: source $workspace_dir/install/malbut_test/local_setup.zsh"
