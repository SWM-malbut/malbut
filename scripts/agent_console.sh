#!/bin/sh
# Run the tracked source console. Models, local databases and venvs stay ignored.
set -eu

agent_console_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
agent_console_root=$(dirname "$agent_console_dir")
if [ -n "${MALBUT_AGENT_CONSOLE_PYTHON:-}" ]; then
    agent_console_python=$MALBUT_AGENT_CONSOLE_PYTHON
elif [ -x "$agent_console_root/.runtime/agent-console/venv/bin/python" ]; then
    agent_console_python="$agent_console_root/.runtime/agent-console/venv/bin/python"
elif [ -x "$agent_console_root/.runtime/fall-voice-20260925/venv/bin/python" ]; then
    agent_console_python="$agent_console_root/.runtime/fall-voice-20260925/venv/bin/python"
else
    agent_console_python=python3
fi
if ! "$agent_console_python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    printf '%s\n' 'Python 3.10 이상이 필요합니다. MALBUT_AGENT_CONSOLE_PYTHON으로 가상환경 Python을 지정하세요.' >&2
    exit 2
fi
cd "$agent_console_root"
exec "$agent_console_python" -u "$agent_console_root/malbut_agent_server/tools/agent_console/console.py" "$@"
