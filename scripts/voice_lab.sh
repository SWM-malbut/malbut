#!/usr/bin/env bash
# Start the isolated source-tree voice laboratory; never launches robot bringup.
set -e

voice_lab_script=$(realpath "${BASH_SOURCE[0]}")
voice_lab_root=$(dirname "$(dirname "$voice_lab_script")")
voice_lab_cache="${XDG_CACHE_HOME:-$HOME/.cache}/malbut-voice-lab"
voice_lab_python="${MALBUT_VOICE_LAB_PYTHON:-/usr/bin/python3}"

if [[ "${1:-}" == "--terminal" ]]; then
    shift
    voice_lab_title='Malbut 음성 명령 실험실'
    for voice_lab_argument in "$@"; do
        if [[ "$voice_lab_argument" == "--chat" ]]; then
            voice_lab_title='Malbut 자유 대화 · Manager 실험실'
        fi
    done
    if command -v gnome-terminal >/dev/null 2>&1; then
        exec gnome-terminal --title="$voice_lab_title" -- "$voice_lab_script" "$@"
    fi
    if command -v x-terminal-emulator >/dev/null 2>&1; then
        exec x-terminal-emulator -T 'Malbut Voice Lab' -e "$voice_lab_script" "$@"
    fi
    printf '새 창을 여는 터미널 프로그램이 없습니다. --terminal 없이 실행하세요.\n' >&2
    exit 2
fi

export PYTHONPATH="$voice_lab_root/malbut_agent_server:$voice_lab_root/malbut_system_manager:${PYTHONPATH:-}"
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" || "${1:-}" == "--list" ]]; then
    exec "$voice_lab_python" -m malbut_agent_server.voice_lab "$@"
fi

voice_lab_ros="/opt/ros/${ROS_DISTRO:-humble}/setup.bash"
if [[ ! -f "$voice_lab_ros" ]]; then
    printf 'ROS 환경을 찾을 수 없습니다: %s\n' "$voice_lab_ros" >&2
    exit 2
fi
source "$voice_lab_ros"
mkdir -p "$voice_lab_cache/python"
export PYTHONPATH="$voice_lab_cache/python:$voice_lab_root/malbut_agent_server:$voice_lab_root/malbut_system_manager:${PYTHONPATH:-}"
export TIKTOKEN_CACHE_DIR="$voice_lab_cache/tiktoken"
export PYTHONUNBUFFERED=1

if ! "$voice_lab_python" -c 'import tiktoken, yaml, pytest' >/dev/null 2>&1; then
    printf '시험용 Python 의존성을 사용자 캐시에 설치합니다.\n'
    "$voice_lab_python" -m pip install --target "$voice_lab_cache/python" 'tiktoken>=0.7,<1' PyYAML pytest || {
        printf 'pip가 있는 Python을 MALBUT_VOICE_LAB_PYTHON으로 지정하거나 필요한 패키지를 설치하세요.\n' >&2
        exit 2
    }
fi

voice_lab_source_hash=$("$voice_lab_python" - "$voice_lab_root" <<'PY'
import hashlib
from pathlib import Path
import sys
root = Path(sys.argv[1]) / 'malbut_interfaces'
hash_value = hashlib.sha256()
for file in sorted(root.rglob('*')):
    if file.is_file() and (file.suffix in {'.action', '.srv', '.msg', '.yaml'} or
                          file.name in {'CMakeLists.txt', 'package.xml'}):
        hash_value.update(str(file.relative_to(root)).encode())
        hash_value.update(file.read_bytes())
print(hash_value.hexdigest()[:16])
PY
)
voice_lab_build="$voice_lab_cache/interfaces-$voice_lab_source_hash"
if [[ ! -f "$voice_lab_build/install/setup.bash" || ! -f "$voice_lab_build/complete" ]]; then
    printf '시험용 ROS 메시지를 별도 캐시에 빌드합니다.\n'
    colcon --log-base "$voice_lab_build/log" build \
        --base-paths "$voice_lab_root/malbut_interfaces" \
        --packages-select malbut_interfaces \
        --build-base "$voice_lab_build/build" \
        --install-base "$voice_lab_build/install" \
        --cmake-args -DBUILD_TESTING=OFF
    touch "$voice_lab_build/complete"
fi
source "$voice_lab_build/install/setup.bash"
export PYTHONPATH="$voice_lab_cache/python:$voice_lab_root/malbut_agent_server:$voice_lab_root/malbut_system_manager:${PYTHONPATH:-}"
cd "$voice_lab_root"
exec "$voice_lab_python" -m malbut_agent_server.voice_lab "$@"
