#!/usr/bin/env bash
# Stop Strata: the python server plus its two engine processes.
set -u
pkill -f 'serve/server[.]py' && { echo "server signalled."; sleep 2; } || echo "no server running."
pkill -f 'strata-vision' && echo "vision process signalled." || true
pkill -f 'strata --serve' && echo "engine signalled." || true
for i in $(seq 1 20); do
	pgrep -f 'serve/server[.]py' >/dev/null || { echo "Strata stopped."; exit 0; }
	sleep 1
done
echo "Server still up after 20s; forcing."
pkill -9 -f 'serve/server[.]py' || true
pkill -9 -f 'strata-vision' || true
pkill -9 -f 'strata --serve' || true
