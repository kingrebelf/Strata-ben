#!/usr/bin/env bash
# Switch to GLM-5.3 Flash on llama.cpp (needs a llama.cpp build from 2026-10-07 or later).
# GLM's template always thinks first (reasoning effort "max" by default), so plain chat requests that say nothing
# about thinking get low effort with a 2048-token thinking budget; clients can still ask for more (reasoning_effort,
# reasoning_budget_tokens) or none (reasoning_effort "none" / enable_thinking false).
. "$(dirname "$(readlink -f "$0")")/llm-common.sh"

start_llama "$GLM_LLAMA" glm-5.3-flash "GLM-5.3 Flash" "$GLM_MODEL" "${GLM_CTX:-16384}" \
	"${GLM_CHAT_DEFAULTS:-{\"reasoning_effort\": \"low\", \"reasoning_budget_tokens\": 2048, \"_effort_map\": {\"medium\": \"high\"\}\}}"
