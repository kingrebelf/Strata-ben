#!/usr/bin/env bash
# Switch to GLM-5.3 Flash on llama.cpp (needs a llama.cpp build from 2026-10-07 or later).
# GLM's template always thinks first (reasoning effort "max" by default), so plain chat requests that say nothing
# about thinking get low effort with a 2048-token thinking budget; clients can still ask for more (reasoning_effort,
# reasoning_budget_tokens) or none (reasoning_effort "none" / enable_thinking false).  Thinking that a client asks
# for without a budget is capped at 8192 tokens (the 2-bit quant can loop in its thinking).
#
# Speed (4x V100 32 GB, UD-Q2_K_XL, 16K context, measured 2026-10-12):
#   -ub 1024 (default here): ~26 tok/s writing, ~190 tok/s prompt reading (vs ~160 with the default -ub 512).
#   GLM_MTP=1 adds the MTP draft head (GLM_MTP_MODEL, on GPU GLM_MTP_DEVICE): +43% writing on predictable text
#   (summaries: ~35 tok/s), -17% on creative text, prompt reading back to ~140 tok/s, and the draft GPU nearly full.
#   Not faster: flash attention / 8-bit KV cache (slower on V100), -sm row (not supported for this model).
. "$(dirname "$(readlink -f "$0")")/llm-common.sh"

extra=(-ub "${GLM_UBATCH:-1024}")
if [ "${GLM_MTP:-0}" = 1 ]; then
	extra+=(--model-draft "${GLM_MTP_MODEL:-$(dirname "$GLM_MODEL")/mtp-GLM-5.3-Flash-Q4_0.gguf}" -ngld 999
	        -devd "${GLM_MTP_DEVICE:-CUDA0}")
fi
start_llama "$GLM_LLAMA" glm-5.3-flash "GLM-5.3 Flash" "$GLM_MODEL" "${GLM_CTX:-16384}" \
	"${GLM_CHAT_DEFAULTS:-{\"reasoning_effort\": \"low\", \"reasoning_budget_tokens\": 2048, \"_effort_map\": {\"medium\": \"high\"\}, \"_max_reasoning_budget\": 8192\}}" \
	"${extra[@]}"
