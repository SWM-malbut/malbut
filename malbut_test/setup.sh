#!/usr/bin/env bash
set -euo pipefail

# Explicit first-time setup; build.sh never installs OS packages or downloads models.
robot_source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${ROS_DISTRO:-}" != humble ]]; then
  echo 'Source ROS 2 Humble and the manufacturer workspace before running setup.sh.' >&2
  exit 1
fi
/usr/bin/python3 -c '
import sys
if sys.version_info[:2] != (3, 10):
    raise SystemExit("Robot speech requires /usr/bin/python3 with Python 3.10.")
'
if ! command -v nvcc >/dev/null && [[ -x /usr/local/cuda/bin/nvcc ]]; then
  export PATH="/usr/local/cuda/bin:$PATH"
fi
if ! command -v nvcc >/dev/null; then
  echo 'Missing nvcc. Prepare the CUDA toolkit matching the existing JetPack, then rerun setup.sh.' >&2
  exit 1
fi

# Include the whole local homecam tree so rosdep recognizes homecam_detector.
package_paths=(
  "$robot_source_dir"/malbut_*
  "$robot_source_dir/malbut_yolo/vendor/yolo_ros/yolo_ros"
  "$robot_source_dir/malbut_yolo/vendor/yolo_ros/yolo_msgs"
  "$robot_source_dir/homecam_agent"
)
if [[ "$EUID" -eq 0 ]]; then
  apt_command=(apt-get)
else
  apt_command=(sudo apt-get)
fi
bash "$robot_source_dir/homecam_agent/scripts/install_dependencies.sh"
"${apt_command[@]}" install -y --no-install-recommends curl python3-pip libportaudio2
rosdep update
rosdep install --from-paths "${package_paths[@]}" \
  --ignore-src -r -y --rosdistro humble \
  --skip-keys 'ament_python python3-torchvision-pip python3-ultralytics-pip'

speech_cache="${XDG_CACHE_HOME:-$HOME/.cache}/malbut_speech"
whisper_source="${WHISPER_CPP_SOURCE_DIR:-$speech_cache/whisper.cpp}"
speech_model="${MALBUT_STT_MODEL_PATH:-$speech_cache/models/ggml-small.bin}"
whisper_revision=da54572229bcf64ba367d96c7ef15770376c4280
model_sha256=1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b
clone_temp=''
model_temp=''
cleanup() {
  [[ -z "$clone_temp" ]] || rm -rf -- "$clone_temp"
  [[ -z "$model_temp" ]] || rm -f -- "$model_temp"
}
trap cleanup EXIT

check_source() {
  local source="$1"
  local revision changes
  revision="$(git -C "$source" rev-parse HEAD)" || return 1
  changes="$(git -C "$source" status --porcelain)" || return 1
  if [[ ! -f "$source/include/whisper.h" ]] ||
     [[ "$revision" != "$whisper_revision" ]] || [[ -n "$changes" ]]; then
    echo "whisper.cpp must be clean at $whisper_revision: $source" >&2
    echo 'Existing files were preserved. Use a separate WHISPER_CPP_SOURCE_DIR if needed.' >&2
    return 1
  fi
}

if [[ -e "$whisper_source" || -L "$whisper_source" ]]; then
  check_source "$whisper_source"
  echo "Reusing pinned whisper.cpp: $whisper_source"
else
  mkdir -p -- "$(dirname -- "$whisper_source")"
  clone_temp="$(mktemp -d "${whisper_source}.tmp.XXXXXX")"
  git clone --no-checkout https://github.com/ggml-org/whisper.cpp.git "$clone_temp/checkout"
  git -C "$clone_temp/checkout" checkout --detach "$whisper_revision"
  check_source "$clone_temp/checkout"
  mv -T -- "$clone_temp/checkout" "$whisper_source"
fi

if [[ -e "$speech_model" || -L "$speech_model" ]]; then
  if ! printf '%s  %s\n' "$model_sha256" "$speech_model" | sha256sum --check --status; then
    echo "STT model checksum mismatch; existing file preserved: $speech_model" >&2
    echo 'Move it aside or set MALBUT_STT_MODEL_PATH to a new file, then rerun setup.sh.' >&2
    exit 1
  fi
  echo "Reusing verified STT model: $speech_model"
else
  mkdir -p -- "$(dirname -- "$speech_model")"
  model_temp="$(mktemp "${speech_model}.part.XXXXXX")"
  curl --fail --location --retry 3 --output "$model_temp" \
    https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin
  if ! printf '%s  %s\n' "$model_sha256" "$model_temp" | sha256sum --check --status; then
    echo 'Downloaded STT model checksum mismatch; no model was installed. Rerun setup.sh.' >&2
    exit 1
  fi
  mv -T -- "$model_temp" "$speech_model"
  model_temp=''
fi

echo 'Robot dependencies and speech assets are ready; CUDA/venv/ROS build is next.'
echo "Run: bash \"$robot_source_dir/build.sh\" --cmake-args -DBUILD_TESTING=OFF"
echo 'Before cloud launch, export OPENAI_API_KEY and the HOMECAM device settings.'
