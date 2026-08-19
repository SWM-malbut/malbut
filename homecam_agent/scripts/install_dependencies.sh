#!/usr/bin/env bash
set -euo pipefail

if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  source /etc/os-release
  if [[ "${ID:-}" != "ubuntu" || "${VERSION_ID:-}" != "22.04" ]]; then
    echo "warning: this dependency set targets Ubuntu 22.04 / ROS 2 Humble" >&2
  fi
fi

if [[ "${EUID}" -eq 0 ]]; then
  APT=(apt-get)
else
  if ! command -v sudo >/dev/null 2>&1; then
    echo "sudo is required when this script is not run as root" >&2
    exit 1
  fi
  APT=(sudo apt-get)
fi

if ! apt-cache show ros-humble-rclcpp >/dev/null 2>&1; then
  echo "ROS 2 Humble apt repository is not configured." >&2
  echo "Install/configure ROS 2 Humble first, then rerun this script." >&2
  exit 1
fi

"${APT[@]}" update
"${APT[@]}" install -y --no-install-recommends \
  build-essential \
  ca-certificates \
  cmake \
  git \
  m4 \
  pkg-config \
  python3-colcon-common-extensions \
  python3-flake8 \
  python3-numpy \
  python3-opencv \
  python3-pytest \
  python3-rosdep \
  python3-venv \
  libcurl4-openssl-dev \
  libgstreamer1.0-dev \
  libgstreamer-plugins-base1.0-dev \
  liblog4cplus-dev \
  libssl-dev \
  nlohmann-json3-dev \
  gstreamer1.0-alsa \
  gstreamer1.0-libav \
  gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-base-apps \
  gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-ugly \
  gstreamer1.0-tools \
  ros-humble-ament-cmake-gtest \
  ros-humble-action-msgs \
  ros-humble-cv-bridge \
  ros-humble-nav-msgs \
  ros-humble-rcl-interfaces \
  ros-humble-rclcpp \
  ros-humble-rclpy \
  ros-humble-sensor-msgs \
  ros-humble-std-msgs

missing_plugins=()
for plugin in \
  appsrc \
  appsink \
  queue \
  videoconvert \
  videoscale \
  videorate \
  x264enc \
  h264parse \
  audiotestsrc \
  alsasrc \
  audioconvert \
  audioresample \
  opusenc \
  opusdec \
  alsasink
do
  if ! gst-inspect-1.0 "${plugin}" >/dev/null 2>&1; then
    missing_plugins+=("${plugin}")
  fi
done

if ((${#missing_plugins[@]} > 0)); then
  echo "warning: missing GStreamer plugins: ${missing_plugins[*]}" >&2
  exit 2
fi

echo "homecam_agent dependencies and required GStreamer plugins are available."
