# Image and 3D generator usage

Strata can show image and 3D generation alongside API-key token usage. Generator counts use their own graph axis and do not get converted into tokens.

The feature is off unless `STRATA_GENERATOR_USAGE_FILE` names a readable UTF-8 JSON Lines file before Strata starts. Strata reads this file when the Monitor usage graph refreshes. It does not write generator events to the API-key token history.

Write one record per completed or failed generator request. `time` is Unix time in seconds. The other fields are:

- `source_type`: the string `generator`.
- `source_id`: a stable identifier for the generator, such as `flux-image-api` or `trellis-3d-api`.
- `source_label`: optional display name.
- `requests`: request count, normally `1`.
- `image_count` and `assets_3d`: whole output counts.
- `generation_seconds`: elapsed generation time.
- `failed_requests`: `1` for a failed request and `0` otherwise.

For example:

```json
{"time":1792541583,"source_type":"generator","source_id":"flux-image-api","source_label":"FLUX.2 Klein 4B image API","requests":1,"image_count":1,"assets_3d":0,"generation_seconds":13.1,"failed_requests":0}
```

Do not include prompts, API keys, or image data in these records. Strata reads only the listed usage fields and keeps generator totals separate from token totals.

