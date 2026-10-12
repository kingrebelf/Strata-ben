#!/usr/bin/env bash
# Start Strata's server (Qwen3.8) behind the model switch: Strata's usual command with its run config, but on
# 127.0.0.1:$STRATA_PORT and without opening a browser.  Called by start-qwen38.sh (which loads llm.conf).
set -u
: "${STRATA_DIR:?run this through start-qwen38.sh}" "${STRATA_CONFIG:?}" "${STRATA_PYTHON:?}" "${STRATA_PORT:?}"
cd "$STRATA_DIR" || exit 1
nohup "$STRATA_PYTHON" "$STRATA_DIR/serve/server.py" --engine strata --config "$STRATA_CONFIG" \
	--host 127.0.0.1 --port "$STRATA_PORT" >> "$STRATA_DIR/strata-server.out" 2>&1 < /dev/null &
echo "Strata starting (pid $!) on 127.0.0.1:$STRATA_PORT ... a cold start takes a minute or two."
