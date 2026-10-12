#!/usr/bin/env bash
# Stop whichever model is running and free the GPUs.  The switch stays up and reports "no model running".
. "$(dirname "$(readlink -f "$0")")/llm-common.sh"

stop_all && echo "All models stopped; GPUs free."
