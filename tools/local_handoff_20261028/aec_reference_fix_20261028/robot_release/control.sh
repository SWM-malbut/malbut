#!/usr/bin/env bash
set -e
release=/home/ubuntu/.local/share/malbut/speech-releases/aec-20261028
unit=malbut-speech-aec.service
case "${1:-status}" in
start|legacy)
    if systemctl --user is-active --quiet "$unit"; then
        echo "Speech is already running; stop it before changing profiles." >&2
        exit 1
    fi
    profile=stt.yaml
    [[ "$1" == legacy ]] && profile=stt-legacy.yaml
    exec systemd-run --user --unit="$unit" --collect \
        --property=Restart=no --property=TimeoutStopSec=10 \
        --setenv="MALBUT_SPEECH_CONFIG=$release/$profile" \
        "$release/start.sh" agent_conversation_db:="$release/conversation.sqlite3"
    ;;
stop) exec systemctl --user stop "$unit" ;;
status) exec systemctl --user status "$unit" --no-pager ;;
*) echo "Usage: $0 {start|stop|legacy|status}" >&2; exit 2 ;;
esac
