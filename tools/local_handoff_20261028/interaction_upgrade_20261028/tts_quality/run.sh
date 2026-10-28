#!/usr/bin/env bash
set -euo pipefail
root=/tmp/malbut_interaction_20261028/tts_quality
set -a
source /home/ubuntu/.config/malbut/openai.env
set +a
/home/ubuntu/.cache/malbut_speech/runtime/bin/python "$root/benchmark.py" piper
/home/ubuntu/.cache/malbut_speech/runtime/bin/python "$root/benchmark.py" openai
