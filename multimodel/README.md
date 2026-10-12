# multimodel - one server on port 8080, one model at a time

See [../README-STRATA-BEN.md](../README-STRATA-BEN.md#one-server-several-models) for the overview and setup.

| File | What it does |
| --- | --- |
| `model-switch.py` | The front on the public port. Forwards to the running model, answers `/v1/active-model`, runs the Decisions API for llama.cpp models, adds the "Running model" line to Strata's Monitor tab. |
| `llm-common.sh` | Shared launcher code: settings from `llm.conf`, stop-before-start, GPU wait, health wait. |
| `start-qwen38.sh` | Qwen3.8 on Strata (`strata-start.sh` runs Strata's server on 127.0.0.1:8181). |
| `start-deepseek-v4-flash.sh`, `start-glm53.sh` | DeepSeek V4 Flash / GLM-5.3 on llama-server, 127.0.0.1:8182. |
| `stop-all-models.sh`, `strata-stop.sh` | Stop the running model / Strata. |
| `llm.conf.example` | Settings template; copy to `llm.conf` (gitignored) next to the installed launchers. |
| `install.sh` | Installs the launchers in `~/llm-launchers` and the Desktop shortcuts. |

State lives in `~/.local/state/llm-launchers/`: `active.json` (the running model, read by the switch), pid files and
logs (`model-switch.log`, one log per llama.cpp model).
