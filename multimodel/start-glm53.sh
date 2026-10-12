#!/usr/bin/env bash
# Switch to GLM-5.3 Flash on llama.cpp (needs a llama.cpp build from 2026-10-07 or later).
. "$(dirname "$(readlink -f "$0")")/llm-common.sh"

start_llama "$GLM_LLAMA" glm-5.3-flash "GLM-5.3 Flash" "$GLM_MODEL" "${GLM_CTX:-16384}"
