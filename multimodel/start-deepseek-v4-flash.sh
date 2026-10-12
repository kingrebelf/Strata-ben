#!/usr/bin/env bash
# Switch to DeepSeek V4 Flash (0731) on llama.cpp.  Measured on 4x V100 32 GB with the Unsloth UD-Q2_K_XL quant:
# ~22.5 tok/s decode, ~139 tok/s prompt reading on a 4K prompt, ~25-27 GB per card at a 16K context.
. "$(dirname "$(readlink -f "$0")")/llm-common.sh"

start_llama "$DS4_LLAMA" deepseek-v4-flash "DeepSeek V4 Flash 0731" "$DS4_MODEL" "${DS4_CTX:-16384}"
