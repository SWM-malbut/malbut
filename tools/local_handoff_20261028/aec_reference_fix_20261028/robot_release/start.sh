#!/usr/bin/env bash
set -e
release=/home/ubuntu/.local/share/malbut/speech-releases/aec-20261028
exec 9>"$release/speech.lock"
flock -n 9 || { echo "This speech release is already running." >&2; exit 1; }
source "$release/environment.sh"
if [[ -f /home/ubuntu/.config/malbut/openai.env ]]; then
    source /home/ubuntu/.config/malbut/openai.env
fi
exec ros2 launch "$release/speech.launch.py"     python_executable:=/usr/bin/python3     stt_model_path:=/home/ubuntu/.cache/malbut_speech/models/ggml-small.bin     stt_library_path:=/home/ubuntu/.cache/malbut_speech/whisper-cpp-build/bin/libmalbut_whisper.so     stt_config:="${MALBUT_SPEECH_CONFIG:-$release/stt.yaml}" tts_config:="$release/tts.yaml"     manager_commands:=false start_manager:=false "$@"
