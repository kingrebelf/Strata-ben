#!/usr/bin/env bash
# Switch to Qwen3.8-Flash-Next on Strata (Strata's server, behind the model switch on 127.0.0.1:$STRATA_PORT).
. "$(dirname "$(readlink -f "$0")")/llm-common.sh"

# already running: keep it, only make sure the switch is up (and on its current code)
if grep -q '"backend": "strata"' "$STATE/active.json" 2>/dev/null &&
		curl -fsS -m 3 "http://127.0.0.1:$STRATA_PORT/health" >/dev/null 2>&1; then
	ensure_switch || exit 1
	echo "Qwen3.8 is already running: http://$PUBLIC_HOST:$PORT/v1"
	exit 0
fi
stop_all || exit 1
ensure_switch || exit 1
if listening "$STRATA_PORT"; then   # another service there would answer for Strata: refuse, pick a free port in llm.conf
	echo "Port $STRATA_PORT is used by another program; set STRATA_PORT in llm.conf to a free port."; exit 1
fi
echo "$SWITCH_NOTE"
"$LAUNCH/strata-start.sh" || exit 1
set_active qwen3.8-flash-next "Qwen3.8 Flash Next (Strata)" strata "http://127.0.0.1:$STRATA_PORT"
if wait_health "$STRATA_PORT" 900; then
	curl -s "http://127.0.0.1:$STRATA_PORT/health"; echo
	echo "Ready: http://$PUBLIC_HOST:$PORT/v1  (running model: Qwen3.8 Flash Next; API key required)"
else
	echo "Still loading after 15 min.  Check: tail -f $STRATA_DIR/strata-server.out"; exit 1
fi
