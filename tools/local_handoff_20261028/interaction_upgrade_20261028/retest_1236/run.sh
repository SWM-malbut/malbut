#!/usr/bin/env bash
set -e
volume=$1
mode=$2
count=${3:-1}
root=/tmp/malbut_interaction_20261028
out=$root/retest_1236
release=/home/ubuntu/.local/share/malbut/speech-releases/interaction-20261028
phase=${mode}_${volume}
cleanup() {
  systemctl --user stop malbut-interaction-live.service || true
  pactl set-sink-volume alsa_output.usb-GeneralPlus_USB_Audio_Device-00.analog-stereo 52428 52428
  systemd-run --user --unit=malbut-speech-interaction.service --collect --property=Restart=no --property=TimeoutStopSec=10 --setenv=MALBUT_SPEECH_GREETING=0 "$release/start.sh" || true
}
trap cleanup EXIT
"$release/control.sh" stop
pactl set-sink-volume alsa_output.usb-GeneralPlus_USB_Audio_Device-00.analog-stereo "$volume%"
export MALBUT_RETEST_RECORD_PREFIX="$out/$phase"
systemd-run --user --unit=malbut-interaction-live.service --collect --property=Restart=no --property=TimeoutStopSec=10 --setenv=ROS_DOMAIN_ID=83 --setenv=ROS_LOCALHOST_ONLY=1 --setenv=MALBUT_RETEST_RECORD_PREFIX="$out/$phase" --setenv=MALBUT_LIVE_STT_CONFIG="$root/live-volume.yaml" "$root/start-live.sh"
source /home/ubuntu/.local/share/malbut/speech-releases/aec-20261028/environment.sh
export ROS_DOMAIN_ID=83 ROS_LOCALHOST_ONLY=1
python3 "$out/probe.py" "$phase" "$mode" "$count" >"$out/$phase.log" 2>&1
cat "$out/$phase.log"
