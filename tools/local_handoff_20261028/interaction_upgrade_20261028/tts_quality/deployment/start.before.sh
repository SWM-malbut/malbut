#!/usr/bin/env bash
set -e
base=/home/ubuntu/.local/share/malbut/speech-releases/aec-20261028
release=/home/ubuntu/.local/share/malbut/speech-releases/interaction-20261028
exec 9>"$base/speech.lock"
flock -n 9 || exit 1
source "$base/environment.sh"
export PYTHONPATH="$release/code:$PYTHONPATH"
export MALBUT_SPEECH_GREETING="${MALBUT_SPEECH_GREETING:-1}"
profile=stt.yaml
conversation_db=/home/ubuntu/.local/state/malbut/speech-dialogue.sqlite3
if [[ "${MALBUT_EXPERIMENTAL_INTERRUPTION_ASR:-0}" == 1 ]]; then
    profile=stt-experimental.yaml
fi
if [[ "${MALBUT_INTERACTION_INJECTION:-0}" == 1 ]]; then
    export ROS_DOMAIN_ID=83 ROS_LOCALHOST_ONLY=1
    export PULSE_SOURCE=malbut_aec_injection_20261028.monitor
    export MALBUT_SHARED_MICROPHONE=$PULSE_SOURCE
    profile=stt-injection.yaml
    conversation_db="$release/conversation-test.sqlite3"
fi
if [[ -f /home/ubuntu/.config/malbut/openai.env ]]; then
    source /home/ubuntu/.config/malbut/openai.env
fi
exec ros2 launch "$base/speech.launch.py" python_executable:=/usr/bin/python3 \
    stt_model_path:=/home/ubuntu/.cache/malbut_speech/models/ggml-small.bin \
    stt_library_path:=/home/ubuntu/.cache/malbut_speech/whisper-cpp-build/bin/libmalbut_whisper.so \
    stt_config:="$release/$profile" tts_config:="$base/tts.yaml" \
    manager_commands:=false start_manager:=false \
    agent_conversation_db:="$conversation_db" "$@"
