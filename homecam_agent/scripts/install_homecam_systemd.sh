#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: sudo $0 --workspace PATH --environment-file PATH [--user USER]" >&2
}

workspace=""
environment_file=""
service_user="malbut"
while (($# > 0)); do
  case "$1" in
    --workspace) workspace="${2:-}"; shift 2 ;;
    --environment-file) environment_file="${2:-}"; shift 2 ;;
    --user) service_user="${2:-}"; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done

if [[ ${EUID} -ne 0 ]]; then
  echo "error: run this installer with sudo" >&2
  exit 1
fi
if [[ -z "$workspace" || -z "$environment_file" ]]; then
  usage
  exit 2
fi
workspace="$(realpath -e "$workspace")"
environment_file="$(realpath -e "$environment_file")"
if [[ ! -f "$workspace/install/setup.bash" ]]; then
  echo "error: ROS workspace is not built: $workspace" >&2
  exit 1
fi
if ! getent passwd "$service_user" >/dev/null; then
  echo "error: service user does not exist: $service_user" >&2
  exit 1
fi
service_group="$(id -gn "$service_user")"
template="$workspace/homecam_agent/systemd/malbut-homecam.service.in"
if [[ ! -f "$template" ]]; then
  echo "error: systemd template not found: $template" >&2
  exit 1
fi
for required_setting in \
  HOMECAM_DEVICE_TOKEN_FILE \
  HOMECAM_BACKEND_URL \
  HOMECAM_DEVICE_ID \
  HOMECAM_IMAGE_TOPIC
do
  if ! grep -Eq "^${required_setting}=.+" "$environment_file"; then
    echo "error: environment file must contain ${required_setting}" >&2
    exit 1
  fi
done
if ! grep -Eq '^HOMECAM_DEVICE_TOKEN_FILE=/.+' "$environment_file"; then
  echo "error: HOMECAM_DEVICE_TOKEN_FILE must be an absolute path" >&2
  exit 1
fi

install -o root -g "$service_group" -m 0640 \
  "$environment_file" /etc/malbut-homecam.env
sed \
  -e "s|@HOMECAM_USER@|$service_user|g" \
  -e "s|@HOMECAM_GROUP@|$service_group|g" \
  -e "s|@ENV_FILE@|/etc/malbut-homecam.env|g" \
  -e "s|@WORKSPACE@|$workspace|g" \
  "$template" >/etc/systemd/system/malbut-homecam.service
chmod 0644 /etc/systemd/system/malbut-homecam.service
systemctl daemon-reload
systemctl enable malbut-homecam.service

echo "installed malbut-homecam.service"
echo "start with: sudo systemctl start malbut-homecam.service"
echo "inspect with: systemctl status malbut-homecam.service"
