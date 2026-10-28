#!/usr/bin/env bash
set -e
release=/home/ubuntu/.local/share/malbut/speech-releases/aec-20261028
trial=/tmp/malbut_interaction_20261028
mods=()
cleanup() {
    systemctl --user stop malbut-interaction-capture.service || true
    for ((i=${#mods[@]}-1;i>=0;i--)); do pactl unload-module "${mods[i]}" || true; done
    "$release/control.sh" start || true
}
trap cleanup EXIT
"$release/control.sh" stop
mods+=("$(pactl load-module module-null-sink sink_name=malbut_aec_injection_20261028 rate=16000 channels=1 channel_map=mono)")
mods+=("$(pactl load-module module-loopback source=alsa_input.usb-iflytek_XFM-DP-V0.0.18_1c001425758447f2650-01.mono-fallback sink=malbut_aec_injection_20261028 latency_msec=50 adjust_time=0 source_dont_move=true sink_dont_move=true)")
systemd-run --user --unit=malbut-interaction-capture.service --collect --property=Restart=no --property=TimeoutStopSec=10 "$trial/start-capture.sh" agent_conversation_db:="$trial/conversation.sqlite3"
source "$release/environment.sh"
export ROS_DOMAIN_ID=83 ROS_LOCALHOST_ONLY=1
python3 /tmp/malbut_aec_fix_20261028/simple_e2e_probe.py >"$trial/capture_probe.log" 2>&1
cp /tmp/malbut_aec_fix_20261028/simple_e2e.json "$trial/capture_result.json"
echo 'Capture finished; restoring normal speech'
