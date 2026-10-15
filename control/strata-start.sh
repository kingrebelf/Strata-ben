#!/usr/bin/env bash
# Start Strata (the local Qwen3.8 server) as a background service.  Launched from
# the "Strata Start" desktop shortcut.  Idempotent: if the server is already up it
# just says so and exits.
set -u

STRATA=/home/max/Desktop/Strata
LOG=$STRATA/strata-server.out
HEALTH=http://127.0.0.1:8080/health

if curl -fsS -m 2 "$HEALTH" >/dev/null 2>&1; then
	echo "Strata is already running."
	exit 0
fi

cd "$STRATA" || exit 1
nohup ./run-iq3_s.sh >> "$LOG" 2>&1 &
echo "Strata starting (pid $!) ... cold start takes a minute or two."

for i in $(seq 1 180); do
	if curl -fsS -m 2 "$HEALTH" >/dev/null 2>&1; then
		curl -s "$HEALTH"
		echo
		echo "Ready:  http://10.0.0.18:8080/v1   (API key required, see \`cat \"/home/max/Desktop/API- Strata\"\`)"
		exit 0
	fi
	sleep 1
done

echo "Still warming up after 180s.  Check:  tail -f \"$LOG\""
exit 1