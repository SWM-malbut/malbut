#!/usr/bin/env bash
set -e
phase=$1
root=/tmp/malbut_interaction_20261028
release=/home/ubuntu/.local/share/malbut/speech-releases/interaction-20261028
cleanup() { systemctl --user stop malbut-interaction-live.service || true; "$release/control.sh" start || true; }
trap cleanup EXIT
"$release/control.sh" stop
systemd-run --user --unit=malbut-interaction-live.service --collect --property=Restart=no --property=TimeoutStopSec=10 --setenv=ROS_DOMAIN_ID=83 --setenv=ROS_LOCALHOST_ONLY=1 --setenv=MALBUT_LIVE_STT_CONFIG="$root/live-$phase.yaml" "$root/start-live.sh"
source /home/ubuntu/.local/share/malbut/speech-releases/aec-20261028/environment.sh
export ROS_DOMAIN_ID=83 ROS_LOCALHOST_ONLY=1
python3 "$root/live_probe.py" "$phase" >"$root/live-$phase.log" 2>&1
