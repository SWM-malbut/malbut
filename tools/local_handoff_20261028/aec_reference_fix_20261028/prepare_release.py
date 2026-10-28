from pathlib import Path
import json,yaml
root=Path('/home/ubuntu/.local/share/malbut/speech-releases/aec-20261028')
source='alsa_input.usb-iflytek_XFM-DP-V0.0.18_1c001425758447f2650-01.mono-fallback'
sink='alsa_output.usb-GeneralPlus_USB_Audio_Device-00.analog-stereo'
stt={'/**':{'ros__parameters':dict(aec_backend='pulse_reference',aec_source=source,aec_sink=sink,aec_model_directory=str(root/'models'),aec_calibration_audio=str(root/'calibration.wav'),barge_in_mode='reachy',turn_detection='legacy',device_index=0)}}
(root/'stt.yaml').write_text(yaml.safe_dump(stt,allow_unicode=True))
tts=yaml.safe_load(Path('/home/ubuntu/.config/malbut/tts.yaml').read_text());tts['/**']['ros__parameters']['output_device_name']='pulse';tts['/**']['ros__parameters']['output_device']=-1
(root/'tts.yaml').write_text(yaml.safe_dump(tts,allow_unicode=True))
(root/'environment.sh').write_text('''#!/usr/bin/env bash
source /opt/ros/humble/setup.bash
source /home/ubuntu/ros2_ws/install/setup.bash
source /home/ubuntu/.local/share/malbut/speech-releases/aec-20261028/install/setup.bash
export PYTHONPATH=/home/ubuntu/.local/share/malbut/speech-releases/aec-20261028/support:/home/ubuntu/.local/share/malbut/speech-releases/aec-20261028/deps:${PYTHONPATH:-}
export PULSE_SOURCE='''+source+'''
export MALBUT_SHARED_MICROPHONE=$PULSE_SOURCE
export PULSE_SINK='''+sink+'''
''')
(root/'start.sh').write_text('''#!/usr/bin/env bash
set -e
release=/home/ubuntu/.local/share/malbut/speech-releases/aec-20261028
source "$release/environment.sh"
if [[ -f /home/ubuntu/.config/malbut/openai.env ]]; then
    source /home/ubuntu/.config/malbut/openai.env
fi
exec ros2 launch "$release/speech.launch.py" \
    python_executable:=/usr/bin/python3 \
    stt_model_path:=/home/ubuntu/.cache/malbut_speech/models/ggml-small.bin \
    stt_library_path:=/home/ubuntu/.cache/malbut_speech/whisper-cpp-build/bin/libmalbut_whisper.so \
    stt_config:="$release/stt.yaml" tts_config:="$release/tts.yaml" \
    manager_commands:=false start_manager:=false "$@"
''')
(root/'start.sh').chmod(0o700)
print('Release configuration prepared; existing config files unchanged')
