# Generator and lifetime token usage

Strata can show image, 3D, and sound generation alongside API-key token usage. Generator counts use their own graph axis and do not get converted into tokens. When a sound event includes a model label, the Monitor shows it as a separate source (for example, MOSS-SoundEffect v2.0 and Stable Audio 3 Small SFX).

The feature is off unless `STRATA_GENERATOR_USAGE_FILE` names a readable UTF-8 JSON Lines file before Strata starts. Strata reads this file when the Monitor usage graph refreshes. It does not write generator events to the API-key token history.

Write one record per completed or failed generator request. `time` is Unix time in seconds. The other fields are:

- `source_type`: the string `generator`.
- `source_id`: a stable identifier for the generator, such as `flux-image-api` or `trellis-3d-api`.
- `source_label`: optional display name.
- `requests`: request count, normally `1`.
- `image_count` and `assets_3d`: whole output counts.
- `sounds`: whole sound output count.
- `model`: optional model name, used to split sound usage by model.
- `generation_seconds`: elapsed generation time.
- `failed_requests`: `1` for a failed request and `0` otherwise.

For example:

```json
{"time":1792541583,"source_type":"generator","source_id":"flux-image-api","source_label":"FLUX.2 Klein 4B image API","requests":1,"image_count":1,"assets_3d":0,"generation_seconds":13.1,"failed_requests":0}
{"time":1792541584,"source_type":"generator","source_id":"sound-api","source_label":"Sound effects","model":"Stable Audio 3 Small SFX","requests":1,"sounds":1,"generation_seconds":8.2,"failed_requests":0}
```

Do not include prompts, API keys, or image data in these records. Strata reads only the listed usage fields and keeps generator totals separate from token totals.

## Lifetime LLM tokens

The Monitor's lifetime total adds prompt and output tokens from successful and interrupted API requests recorded by Strata. It is stored in `api-usage-lifetime.json` beside Strata's private API usage state, separate from the 90-day chart history. The file contains only aggregate counts and is written with owner-only permissions where the operating system supports them.

On first startup with this feature, Strata backfills from the usage events still available on disk. Earlier events stored only reused (cached) prompt-token counts, so those old records cannot reconstruct all input tokens. Full prompt and output counting starts at the upgrade time shown by the Monitor. Images, 3D assets, and sounds stay in their own count/time units and are not included in the token total.

When moving Strata to another machine, preserve `api-usage-lifetime.json` with the private Strata state if the lifetime total should carry over. Do not commit it to a public repository or include it in a shared bundle.

## GPU service labels

Set `STRATA_GPU_ROLES_FILE` to a local JSON profile to show per-GPU service cards on the Monitor. The file is keyed by `nvidia-smi` GPU index and supplies display-only role and service labels. For the four-V100 Max AI layout in the example, copy [gpu-roles.max-ai.example.json](../deploy/gpu-roles.max-ai.example.json) to a private configuration path (for example `/etc/strata/gpu-roles.json`), set `STRATA_GPU_ROLES_FILE` in Strata's service environment, and restart Strata. The profile contains no tunnel addresses or credentials.

