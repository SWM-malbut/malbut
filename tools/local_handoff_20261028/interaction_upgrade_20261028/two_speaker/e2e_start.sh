#!/usr/bin/env bash
set -e
base=/home/ubuntu/.local/share/malbut/speech-releases/aec-20261028
release=/home/ubuntu/.local/share/malbut/speech-releases/interaction-20261028
root=/tmp/malbut_interaction_20261028/two_speaker
exec 9>"$base/speech.lock"
flock -n 9 || exit 1
source "$base/environment.sh"
export PYTHONPATH="$root/e2e-code:$release/code:$PYTHONPATH"
export MALBUT_SPEECH_GREETING=0
source /home/ubuntu/.config/malbut/openai.env
exec ros2 launch "$base/speech.launch.py" python_executable:=/usr/bin/python3 \
 stt_model_path:=/home/ubuntu/.cache/malbut_speech/models/ggml-small.bin \
 stt_library_path:=/home/ubuntu/.cache/malbut_speech/whisper-cpp-build/bin/libmalbut_whisper.so \
 stt_config:="$release/stt.yaml" tts_config:="$release/tts.yaml" \
 manager_commands:=false start_manager:=false agent_conversation_db:="$MALBUT_TWO_SPEAKER_DB"
