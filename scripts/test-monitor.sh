#!/bin/bash

set -euo pipefail
IFS=$'\n\t'

PROCESS_NAME="test"
LOG_FILE="/var/log/monitoring.log"
PID_FILE="/tmp/${PROCESS_NAME}_pid"
MONITOR_URL="https://test.com/monitoring/test/api"

touch "$LOG_FILE"
chmod 644 "$LOG_FILE"

timestamp() { date "+%Y-%m-%d %H:%M:%S"; }
log() { echo "$(timestamp) — $*" >> "$LOG_FILE"; }

check_server() { curl -fs --max-time 10 "$MONITOR_URL" -o /dev/null || return 1; }

main() {
    current_pid=$(pgrep -x "$PROCESS_NAME" || true)

    if [[ -n "$current_pid" ]]; then
        if [[ -f "$PID_FILE" ]]; then
            prev_pid=$(<"$PID_FILE")
            if [[ "$current_pid" != "$prev_pid" && -n "$prev_pid" ]]; then
                log "Process '$PROCESS_NAME' restarted"
            fi
        fi

        echo "$current_pid" > "$PID_FILE"

        if ! check_server; then
            log "Monitoring server unavailable"
        fi
    fi
}

main "$@"
