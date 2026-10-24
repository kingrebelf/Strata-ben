# Moving a customized Strata server

This guide prepares a Strata host and its local model services for a move or a shared deployment. It keeps the deployable code and service map separate from private configuration, tunnel credentials, model weights, and generated files.

## Check the destination first

From a Strata checkout, inspect the destination GPUs before copying model files or enabling services:

```sh
python tools/strata_migration.py preflight --profile deploy/stack.max-ai.example.json
```

The standard Strata build requires NVIDIA compute capability 7.5 or newer. Some older NVIDIA cards can use the documented experimental CUDA 12.x build from compute capability 6.0. A Tesla M40 is compute capability 5.2, below both floors, so it cannot run this Strata engine. The tool reports the capability of every visible NVIDIA GPU and checks the profile's assigned indices. The image, 3D, and sound services also need separate compatibility checks for their own model runtimes.

NVIDIA's [legacy GPU capability table](https://developer.nvidia.com/cuda-gpus/legacy) lists Tesla M40 at 5.2. Strata's [older GPU guide](OLDER_GPUS.md) describes the experimental 6.0 floor.

## Capture a sanitized handoff

The example profile records service unit names, roles, and GPU indices. Copy it and the GPU role file to a private configuration folder before changing anything for the destination. Add the destination's actual Strata and tunnel unit names to the profile if those services run under systemd.

```sh
python tools/strata_migration.py export --profile deploy/stack.max-ai.example.json --output ./strata-handoff
```

The export contains a hardware inventory, current Strata revision, sanitized service profile, GPU display labels, and an environment template. It omits API keys, tunnel hostnames and credentials, service-file contents, prompts, usage events, logs, model weights, and generated files. Review the generated files before sharing them.

If you want the lifetime LLM token total to follow your private migration bundle, opt in explicitly:

```sh
python tools/strata_migration.py export --profile deploy/stack.max-ai.example.json --output ./private-handoff --include-lifetime-usage
```

That writes only aggregate token and request counts plus the counter's recovery marker into `private/api-usage-lifetime.json`. Keep that output private. Do not commit it or send it with a public distribution. To import it on the destination, set `STRATA_LIFETIME_USAGE_FILE` to that file's private path. API keys are intentionally not migrated; create new keys on the destination.

## Install and start the services

The `start`, `stop`, and `status` commands operate on systemd user units named in the selected profile:

```sh
python tools/strata_migration.py status --profile /path/to/stack-profile.json
python tools/strata_migration.py start --profile /path/to/stack-profile.json
python tools/strata_migration.py stop --profile /path/to/stack-profile.json
```

They do not install dependencies, download model weights, create systemd units, or change firewall and tunnel settings. Install each model service from its own source, review its license, restore the local model cache, write and enable its unit on the destination, and then add that unit to the private profile. The checked-in Max AI example lists the FLUX, TRELLIS, MOSS, Stable Audio, and generated-file sharing units observed on the current host; add your Strata server unit and tunnel unit if you want the same start command to manage them. The tunnel list starts empty because tunnel unit names vary by host.

The `STRATA_GPU_ROLES_FILE` profile is display-only. It controls the GPU service cards on the Monitor page; it does not assign GPUs to model processes. Set GPU allocation in each service's own configuration. Edit the example GPU map for the target, then set the environment variable in Strata's local service configuration.

## Reconnect public access

Treat a move as a new tunnel installation. Authenticate the tunnel client on the destination, create or adopt a tunnel under the intended account, route the hostnames to the new loopback services, and recreate any access policies and firewall rules. Store the new tunnel credentials only in the tunnel provider's private configuration directory with restrictive permissions. Do not copy a tunnel credential file into the repository or migration bundle. Keep model services bound to loopback unless you have deliberately configured authenticated network access.

Use a private environment file or service manager for API keys, public hostnames, and local state paths. The exported `.env.example` contains placeholders only. Recreate the destination's API keys and external access policy after the services pass local health checks.

## Share a runnable distribution

Share the source fork, a customized profile without secrets, and setup instructions. Recipients install model runtimes and weights separately and accept the terms for each model. The bundle is not self-contained: model weights and third-party runtime packages are large and may have separate licenses. A working profile plus installed systemd user units lets `strata_migration.py start` bring up the listed processes together; it does not automate model downloads or provider account setup.

The example role map for the current four-V100 host labels GPU 3 as `OpenJev + MOSS-SoundEffect v2.0 + Stable Audio 3 Small SFX`. On another GPU layout, update role labels and service GPU indices together before starting the stack.
