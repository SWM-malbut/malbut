#!/usr/bin/env bash
set -e
release="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
base=/home/ubuntu/.local/share/malbut/speech-releases/aec-20261028
normal=/home/ubuntu/.local/share/malbut/speech-releases/interaction-20261028
source "$base/environment.sh"
export ALSA_CONFIG_PATH="$release/pulse-output-only.conf"
export PYTHONPATH="$release/code:$normal/code:$PYTHONPATH"
export MALBUT_SPEECH_GREETING="${MALBUT_SPEECH_GREETING:-1}"
export MALBUT_STT_INCIDENT_CONFIG='{"enabled":false}'
unset MALBUT_STT_DIAGNOSTIC_DIR MALBUT_STT_SHADOW_VAD_MODEL
# Recording is off by default. An explicit prefix is inherited by bridge and ROS.
if [[ -f /home/ubuntu/.config/malbut/openai.env ]]; then
    source /home/ubuntu/.config/malbut/openai.env
fi
exec /usr/bin/python3 "$release/supervisor.py" "$@"
