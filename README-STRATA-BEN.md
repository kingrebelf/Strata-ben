# Strata-Ben edition

This branch is [Strata](https://github.com/Niko1221/Strata) plus two additions:

1. **A Decisions API** (`/v1/decide`, alias `/v1/systemone`): typed questions about a piece of state, answered with
   probabilities and a confidence value instead of free text.
2. **One server, several models** (`multimodel/`): port 8080 always serves the same API, and you choose which single
   model runs behind it: Qwen3.8-Flash-Next on the Strata engine, or DeepSeek V4 Flash / GLM-5.3 Flash on llama.cpp.
   Only one model runs at a time; switching stops the running one first.

Everything else is upstream Strata, unchanged. The `main` branch mirrors upstream; this edition lives on the
`strata-ben` branch.

## Decisions API

`POST /v1/decide` (or `/v1/systemone`) with the same API key as the rest of the server.

```json
{
  "state": "Customer: I was charged twice for the same invoice this month.",
  "questions": {
    "route":    {"type": "choice", "instructions": "Which team owns this?",
                 "criteria": {"billing": "payments and refunds", "bug": "a product defect", "account": "login and plan"}},
    "urgency":  {"type": "score", "instructions": "How urgent is this?",
                 "criteria": ["routine", "today", "urgent", "critical"]},
    "escalate": {"type": "noul", "instructions": "Should a human step in now?"}
  }
}
```

Answer (abridged):

```json
{
  "model": "qwen3.8-flash-next-iq3_s",
  "answers": {
    "route":    {"type": "choice", "choice": "billing", "probabilities": {"billing": 0.9, "bug": 0.05, "account": 0.05},
                 "confidence": 0.9},
    "urgency":  {"type": "score", "score": 1.4, "legend": ["routine", "today", "urgent", "critical"],
                 "probabilities": {"0": 0.1, "1": 0.45, "2": 0.4, "3": 0.05}, "confidence": 0.45},
    "escalate": {"type": "noul", "noul": 0.3}
  },
  "usage": {"input_tokens": 312, "output_tokens": 96}
}
```

| Question type | `criteria` | Answer fields |
| --- | --- | --- |
| `choice` | object of option labels to short descriptions (1-255) | `choice`, `probabilities`, `confidence` |
| `score` | ordered list of 2-10 levels, low to high | `score`, `legend`, `probabilities`, `confidence` |
| `noul` | optional `"true"` / `"false"` descriptions | `noul` (probability of yes) |

All questions are answered in one structured (JSON-schema) generation, then validated and normalised. Errors:
400 bad request or over the input budgets (32K tokens for state + longest question, 64K in total), 401 missing key,
502 the model's answer did not validate, 503 the model is not ready. The full reference is
[docs/decisions-api.html](docs/decisions-api.html) (`python3 docs/serve-docs.py` serves it on port 4747).

The probabilities are the model's own estimates, not a calibrated classifier's: gate on `confidence` with thresholds
that suit your use. The request and answer format follows the "System One" (Jev) decision API, so clients written for
that API work unchanged.

Code: [serve/systemone.py](serve/systemone.py) (request checks, prompt, JSON schema, answer normalisation) and the
`/v1/systemone` route in `serve/server.py`.

## One server, several models

```
clients ──> :8080  model-switch.py  ──>  Qwen3.8      Strata serve/server.py   127.0.0.1:8181
                   (always running)  or  DeepSeek V4  llama-server             127.0.0.1:8182
                                     or  GLM-5.3      llama-server             127.0.0.1:8182
```

- **Same API on every model**: `/v1/chat/completions`, `/v1/messages`, `/v1/responses`, `/v1/decide`,
  `/v1/systemone`, `/health`, with the same API key (read from Strata's run config). On Qwen3.8 every request goes
  straight to Strata. On the llama.cpp models the switch checks the key, forwards the OpenAI/Anthropic APIs to
  llama-server, and runs the Decisions API itself with the same `serve/systemone.py` and llama.cpp's JSON-schema
  output, so answers come back in the same format.
- **Which model is running**: `GET /v1/active-model` (no key needed) returns `{"name", "label", "backend", "since",
  "healthy"}`. Strata's web UI shows a **Running model** line in the Monitor tab (added by the switch as the page passes
  through, so Strata's own files are not changed). While a llama.cpp model runs, `/` shows a small status page.
- **One model at a time**: each launcher stops the running model, waits until the GPUs are free, then starts its
  model. `stop-all-models.sh` stops everything; the switch stays up and reports "no model running". A switch takes a
  few minutes when the new model has to be read from disk (the previous one pushed it out of the RAM cache).
- **Thinking on the llama.cpp models**: the thinking comes back in `message.reasoning_content`, the answer in
  `message.content`. GLM-5.3's template always thinks first, so a chat request that says nothing about thinking gets
  low effort with a 2048-token thinking budget (`GLM_CHAT_DEFAULTS` changes this). Ask for none with
  `"reasoning_effort": "none"` or `"chat_template_kwargs": {"enable_thinking": false}` (the switch then also sends a
  zero thinking budget), or for more with `"reasoning_effort": "high"` / `"reasoning_budget_tokens": N`. DeepSeek V4
  Flash answers without thinking unless asked (`"chat_template_kwargs": {"thinking": true}`). The Decisions API never
  thinks first.

### Setup

1. Build or install what you want to run: Strata as usual (its `setup.sh`), and a llama.cpp `llama-server` with the
   `deepseek4` and/or `glm5-next` architectures (upstream llama.cpp from 2026-10-07 or later has both).
2. Download the models, for example Unsloth's `DeepSeek-V4-Flash-0731` `UD-Q2_K_XL` (3 files, ~92 GB) and
   `GLM-5.3-Flash` `UD-Q2_K_XL` (4 files, ~101 GiB).
3. `bash multimodel/install.sh` - copies the launchers to `~/llm-launchers`, creates `~/llm-launchers/llm.conf` from
   [multimodel/llm.conf.example](multimodel/llm.conf.example) and puts four shortcuts on the Desktop
   (Qwen3.8, DeepSeek V4 Flash, GLM 5.3 Flash, Stop All Models).
4. Edit `~/llm-launchers/llm.conf`: the Strata directory and run config, the llama-server binaries, the model files,
   the GPUs, the ports. It stays on your machine (`llm.conf` is gitignored).
5. Start a model with a shortcut or `~/llm-launchers/start-qwen38.sh`, `start-deepseek-v4-flash.sh`,
   `start-glm53.sh`; stop with `stop-all-models.sh`. Strata's own run script (`run-*.sh`) binds 8080 directly, so
   use the launchers instead of it once the switch is installed.

To let another account (an automation user) switch models without a password, a sudoers rule limited to the launcher
scripts works, e.g. `automation ALL=(modeluser) NOPASSWD: /home/modeluser/llm-launchers/start-qwen38.sh, ...`; keep the
launcher directory owned by the model user and not writable by the other account.

### Tested hardware

4x Tesla V100 32 GB (Volta, sm_70), 2x Xeon E5-2699 v3, 283 GB RAM, CUDA 12.4, gcc-13 for CUDA builds.

| Model | Engine | Notes |
| --- | --- | --- |
| Qwen3.8-Flash-Next IQ3_S | Strata (4 GPUs, layer split) | the default |
| DeepSeek V4 Flash 0731 UD-Q2_K_XL | llama.cpp, all 4 GPUs | ~22.5 tok/s decode, ~139 tok/s prompt (4K), ~25-27 GB per card at 16K context |
| GLM-5.3 Flash UD-Q2_K_XL | llama.cpp (2026-10-07+), all 4 GPUs | ~101 GiB of weights; start with a 16K context (not benchmarked yet) |

## Credits and licenses

- Strata by [Niko1221](https://github.com/Niko1221/Strata) and contributors, MIT license (see [LICENSE](LICENSE));
  this edition keeps that license for its additions.
- llama.cpp by ggml-org (MIT), used as a separate program.
- The models (Qwen3.8, DeepSeek V4, GLM-5.3) and their quantisations belong to their authors and are under their own
  licenses; this repository contains no model weights.
