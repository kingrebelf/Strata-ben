#!/usr/bin/env bash
# Shared helpers for the model launchers (see multimodel/README.md).
#
# Layout: the public port (8080) is always model-switch.py, a thin front.  Exactly ONE model runs behind it:
#   Qwen3.8 = Strata's serve/server.py on 127.0.0.1:$STRATA_PORT, or DeepSeek / GLM = llama-server on
#   127.0.0.1:$LLAMA_PORT.  Every start script stops the running model first.
# Settings come from llm.conf next to this file (copy llm.conf.example).
set -u

LAUNCH=$(dirname "$(readlink -f "$0")")
[ -f "$LAUNCH/llm.conf" ] || { echo "Missing $LAUNCH/llm.conf - copy llm.conf.example and edit it."; exit 1; }
# shellcheck source=llm.conf.example
. "$LAUNCH/llm.conf"
SERVICE_USER=${SERVICE_USER:-}
if [ -n "$SERVICE_USER" ]; then
	[ "$(id -un)" = "$SERVICE_USER" ] || { echo "These launchers must run as $SERVICE_USER, not $(id -un)."; exit 1; }
	export HOME=$(getent passwd "$SERVICE_USER" | cut -d: -f6)   # `sudo -u` keeps the caller's HOME otherwise
	. "$LAUNCH/llm.conf"                                          # again, so $HOME in it is the service user's
fi
export PATH=/usr/local/cuda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
cd "$HOME" || exit 1

PORT=${PORT:-8080}
STRATA_PORT=${STRATA_PORT:-8181}
LLAMA_PORT=${LLAMA_PORT:-8182}
PUBLIC_HOST=${PUBLIC_HOST:-localhost}
CUDA_DEVICES=${CUDA_DEVICES:-0,1,2,3}
STRATA_PYTHON=${STRATA_PYTHON:-$STRATA_DIR/.venv/bin/python}
STATE=$HOME/.local/state/llm-launchers
mkdir -p "$STATE"
export STRATA_DIR STRATA_CONFIG STRATA_PYTHON STRATA_PORT

listening() { ss -ltn 2>/dev/null | grep -q ":$1 "; }

# model-switch.py on the public port: started once, it stays up between model switches
ensure_switch() {
	if [ -s "$STATE/switch.pid" ] && kill -0 "$(cat "$STATE/switch.pid")" 2>/dev/null; then return 0; fi
	if listening "$PORT"; then
		echo "Port $PORT is taken by something else (not the model switch):"; ss -ltnp | grep ":$PORT "; return 1
	fi
	nohup "$STRATA_PYTHON" -I "$LAUNCH/model-switch.py" --port "$PORT" --state "$STATE/active.json" \
		--strata-dir "$STRATA_DIR" --strata-config "$STRATA_CONFIG" >> "$STATE/model-switch.log" 2>&1 < /dev/null &
	echo $! > "$STATE/switch.pid"
	for i in $(seq 1 20); do listening "$PORT" && return 0; sleep 0.5; done
	echo "The model switch did not start; see $STATE/model-switch.log"; return 1
}

set_active() {   # set_active <name> <label> <strata|llama> <url>
	printf '{"name": "%s", "label": "%s", "backend": "%s", "url": "%s", "since": %s}\n' \
		"$1" "$2" "$3" "$4" "$(date +%s)" > "$STATE/active.json.tmp"
	mv "$STATE/active.json.tmp" "$STATE/active.json"
}

# Stop the running model (llama-server via its pidfile, or Strata), then wait until the GPUs are free.
stop_all() {
	rm -f "$STATE/active.json"
	if [ -s "$STATE/llama-server.pid" ]; then
		pid=$(cat "$STATE/llama-server.pid")
		if kill -0 "$pid" 2>/dev/null && grep -q llama-server "/proc/$pid/cmdline" 2>/dev/null; then
			echo "Stopping llama-server (pid $pid) ..."
			kill "$pid"
			for i in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
			kill -0 "$pid" 2>/dev/null && kill -9 "$pid"
		fi
		rm -f "$STATE/llama-server.pid"
	fi
	if pgrep -f 'serve/server[.]py|strata --serve|strata-vision' >/dev/null; then
		echo "Stopping Strata ..."
		"$LAUNCH/strata-stop.sh"
	fi
	wait_gpus_free
}

# The models are gone from the GPUs (other GPU users are ignored).
wait_gpus_free() {
	for i in $(seq 1 60); do
		if ! nvidia-smi --query-compute-apps=process_name --format=csv,noheader 2>/dev/null |
				grep -qE 'strata|llama-server'; then
			return 0
		fi
		sleep 1
	done
	echo "A model still holds GPU memory after 60 s:"
	nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv
	return 1
}

wait_health() {   # wait_health <port> <seconds> [pid]
	for i in $(seq 1 "$2"); do
		if curl -fsS -m 2 "http://127.0.0.1:$1/health" >/dev/null 2>&1; then return 0; fi
		[ -n "${3:-}" ] && ! kill -0 "$3" 2>/dev/null && return 1
		sleep 1
	done
	return 1
}

# start_llama <llama-server> <name> <label> <model.gguf> <ctx> [extra llama-server args...]
start_llama() {
	local llama=$1 name=$2 label=$3 model=$4 ctx=$5; shift 5
	[ -x "$llama" ] || { echo "llama-server not found: $llama"; return 1; }
	[ -s "$model" ] || { echo "Model not found: $model"; return 1; }
	stop_all || return 1
	ensure_switch || return 1
	listening "$LLAMA_PORT" && { echo "Port $LLAMA_PORT is in use:"; ss -ltnp | grep ":$LLAMA_PORT "; return 1; }
	echo "Starting $label (a cold load can take ~10 min) ..."
	CUDA_VISIBLE_DEVICES=$CUDA_DEVICES nohup "$llama" -m "$model" -ngl 999 -c "$ctx" --host 127.0.0.1 \
		--port "$LLAMA_PORT" --jinja -np 1 --no-webui --alias "$name" "$@" >> "$STATE/$name.log" 2>&1 < /dev/null &
	local pid=$!
	echo "$pid" > "$STATE/llama-server.pid"
	set_active "$name" "$label" llama "http://127.0.0.1:$LLAMA_PORT"
	if wait_health "$LLAMA_PORT" 900 "$pid"; then
		echo "Ready: http://$PUBLIC_HOST:$PORT/v1  (running model: $label; same API key as Strata)"
	else
		echo "Not ready. Last log lines:"; tail -n 20 "$STATE/$name.log"; return 1
	fi
}
