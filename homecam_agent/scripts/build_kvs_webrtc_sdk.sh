#!/usr/bin/env bash
set -euo pipefail

readonly sdk_tag="v1.19.1"
readonly sdk_commit="d7322f63af3c600ee7031b28436e3f8a12664272"
readonly sdk_repository="https://github.com/awslabs/amazon-kinesis-video-streams-webrtc-sdk-c.git"

if [[ $# -ne 1 || -z "$1" ]]; then
  echo "usage: $0 /absolute/path/to/kvs-webrtc-sdk" >&2
  exit 2
fi

sdk_root="$(realpath -m -- "$1")"
if [[ "$sdk_root" != /* || "$sdk_root" == "/" ]]; then
  echo "SDK target must be a specific absolute path, not /" >&2
  exit 2
fi

if [[ -e "$sdk_root" && ! -d "$sdk_root/.git" ]]; then
  echo "target exists but is not a Git checkout: $sdk_root" >&2
  exit 2
fi

if [[ ! -d "$sdk_root/.git" ]]; then
  mkdir -p -- "$(dirname -- "$sdk_root")"
  git clone \
    --branch "$sdk_tag" \
    --depth 1 \
    "$sdk_repository" \
    "$sdk_root"
fi

actual_commit="$(git -C "$sdk_root" rev-parse HEAD)"
if [[ "$actual_commit" != "$sdk_commit" ]]; then
  echo "refusing unpinned SDK checkout: expected $sdk_commit, got $actual_commit" >&2
  exit 2
fi
if ! git -C "$sdk_root" diff --quiet ||
  ! git -C "$sdk_root" diff --cached --quiet
then
  echo "refusing modified SDK checkout: $sdk_root" >&2
  exit 2
fi

sdk_ca_cert="$sdk_root/certs/cert.pem"
if [[ ! -r "$sdk_ca_cert" ]]; then
  echo "pinned SDK CA certificate is missing: $sdk_ca_cert" >&2
  exit 2
fi

cmake \
  -S "$sdk_root" \
  -B "$sdk_root/build" \
  -DBUILD_SAMPLE=OFF \
  -DBUILD_TEST=OFF \
  -DBUILD_BENCHMARK=OFF \
  -DBUILD_DEPENDENCIES=ON \
  -DPARALLEL_BUILD=ON \
  -DCMAKE_BUILD_TYPE=Release \
  "-DKVS_CA_CERT_PATH=$sdk_ca_cert"
cmake --build "$sdk_root/build" --parallel

cat <<EOF
Pinned AWS KVS WebRTC SDK is ready.

KVS_WEBRTC_SDK_ROOT=$sdk_root
AWS_KVS_CACERT_PATH=$sdk_ca_cert

Build the ROS package with:
  colcon build --packages-select homecam_media_agent --cmake-force-configure \\
    --cmake-args -DHOMECAM_ENABLE_KVS=ON \\
    -DKVS_WEBRTC_SDK_ROOT=$sdk_root
EOF
