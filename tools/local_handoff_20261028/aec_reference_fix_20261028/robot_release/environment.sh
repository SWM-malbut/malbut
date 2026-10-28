#!/usr/bin/env bash
source /opt/ros/humble/setup.bash
source /home/ubuntu/ros2_ws/install/setup.bash
source /home/ubuntu/.local/share/malbut/speech-releases/aec-20261028/install/setup.bash
export PYTHONPATH=/home/ubuntu/.local/share/malbut/speech-releases/aec-20261028/support:/home/ubuntu/.local/share/malbut/speech-releases/aec-20261028/deps:${PYTHONPATH:-}
export PULSE_SOURCE=alsa_input.usb-iflytek_XFM-DP-V0.0.18_1c001425758447f2650-01.mono-fallback
export MALBUT_SHARED_MICROPHONE=$PULSE_SOURCE
export PULSE_SINK=alsa_output.usb-GeneralPlus_USB_Audio_Device-00.analog-stereo
