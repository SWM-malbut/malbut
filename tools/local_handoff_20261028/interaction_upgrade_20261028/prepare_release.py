from pathlib import Path
import tarfile,yaml
base=Path('/home/ubuntu/.local/share/malbut/speech-releases/aec-20261028')
root=Path('/home/ubuntu/.local/share/malbut/speech-releases/interaction-20261028')
root.mkdir(parents=True,exist_ok=True)
with tarfile.open('/tmp/malbut_interaction_20261028/release_code.tar.gz') as archive:
    archive.extractall(root)
config=yaml.safe_load((base/'stt.yaml').read_text())
params=config['/**']['ros__parameters']
params.update(interruption_asr='local',interruption_asr_timeout_s=5.,barge_in_pre_roll_s=2.)
(root/'stt.yaml').write_text(yaml.safe_dump(config))
params['interruption_asr']='openai'
(root/'stt-experimental.yaml').write_text(yaml.safe_dump(config))
params['aec_source']='malbut_aec_injection_20261028.monitor'
(root/'stt-injection.yaml').write_text(yaml.safe_dump(config))
start='''#!/usr/bin/env bash
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
exec ros2 launch "$base/speech.launch.py" python_executable:=/usr/bin/python3 \\
    stt_model_path:=/home/ubuntu/.cache/malbut_speech/models/ggml-small.bin \\
    stt_library_path:=/home/ubuntu/.cache/malbut_speech/whisper-cpp-build/bin/libmalbut_whisper.so \\
    stt_config:="$release/$profile" tts_config:="$base/tts.yaml" \\
    manager_commands:=false start_manager:=false \\
    agent_conversation_db:="$conversation_db" "$@"
'''
(root/'start.sh').write_text(start);(root/'start.sh').chmod(0o700)
control='''#!/usr/bin/env bash
set -e
release=/home/ubuntu/.local/share/malbut/speech-releases/interaction-20261028
case "${1:-status}" in
start)
    if systemctl --user is-active --quiet malbut-speech-aec.service; then
        echo 'Stop the previous speech release first.' >&2; exit 1
    fi
    exec systemd-run --user --unit=malbut-speech-interaction.service --collect \\
        --property=Restart=no --property=TimeoutStopSec=10 "$release/start.sh"
    ;;
stop) exec systemctl --user stop malbut-speech-interaction.service ;;
status) exec systemctl --user status malbut-speech-interaction.service --no-pager ;;
rollback)
    systemctl --user stop malbut-speech-interaction.service || true
    exec /home/ubuntu/.local/share/malbut/speech-releases/aec-20261028/control.sh start
    ;;
*) exit 2 ;;
esac
'''
(root/'control.sh').write_text(control);(root/'control.sh').chmod(0o700)
print('Isolated interaction release prepared; not started')
