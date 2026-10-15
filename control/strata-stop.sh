#!/usr/bin/env bash
# Stop Strata: the python server plus its two engine processes (main + vision
# mmproj).  Launched from the "Strata Stop" desktop shortcut.  Idempotent.
set -u

LOG=/home/max/Desktop/Strata/strata-server.out

[ -s "$LOG" ] || touch "$LOG"

# The bracket patterns keep each pattern from matching this script's own command line.
pkill -f 'serve/server[.]py' && { echo "server signalled."; sleep 2; } || echo "no server running."
pkill -f 'strata-vision' && echo "vision process signalled." || true
pkill -f 'strata --serve' && echo "engine signalled." || true

echo "---- waiting for exit ----"
for i in $(seq 1 20); do
	if ! pgrep -f 'serve/server[.]py' >/dev/null; then
		echo "Strata stopped."
		echo "Last log lines:"
		tail -n 12 "$LOG"
		exit 0
	fi
	sleep 1
done

echo "Server still up after 20s; forcing."
pkill -9 -f 'serve/server[.]py' || true
pkill -9 -f 'strata-vision' || true
pkill -9 -f 'strata --serve' || true
echo "Forced stop."