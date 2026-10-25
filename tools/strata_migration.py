#!/usr/bin/env python3
"""Migration helpers for a Strata installation and its local model services.

The export is an inventory and a clean profile template, not a backup. It never copies
model weights, prompts, generated assets, API keys, service files, logs, or tunnel credentials.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = ROOT / "deploy" / "stack.max-ai.example.json"
UNIT_RE = re.compile(r"^[A-Za-z0-9_.@-]+\.service$")


def load_profile(path: Path) -> dict:
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read profile {path}: {exc}") from exc
    if not isinstance(profile, dict):
        raise ValueError("The profile must be a JSON object.")
    services = profile.get("services", [])
    tunnels = profile.get("tunnel_units", [])
    strata_indices = profile.get("strata_gpu_indices", [])
    if (not isinstance(services, list) or not isinstance(tunnels, list) or not isinstance(strata_indices, list) or
            any(isinstance(i, bool) or not isinstance(i, int) or i < 0 for i in strata_indices)):
        raise ValueError("Profile services and tunnel_units must be JSON arrays.")
    seen = set()
    for item in services:
        if not isinstance(item, dict) or not isinstance(item.get("unit"), str):
            raise ValueError("Each service must have a systemd user unit name.")
        unit = item["unit"]
        if not UNIT_RE.fullmatch(unit) or unit in seen:
            raise ValueError(f"Invalid or duplicate service unit: {unit!r}")
        seen.add(unit)
        indices = item.get("gpu_indices", [])
        if (not isinstance(indices, list) or any(isinstance(i, bool) or not isinstance(i, int) or i < 0
                                                 for i in indices)):
            raise ValueError(f"gpu_indices for {unit} must be a list of non-negative integers.")
    for unit in tunnels:
        if not isinstance(unit, str) or not UNIT_RE.fullmatch(unit) or unit in seen:
            raise ValueError(f"Invalid or duplicate tunnel unit: {unit!r}")
        seen.add(unit)
    return profile


def inventory_gpus() -> list[dict]:
    if shutil.which("nvidia-smi") is None:
        return []
    command = ["nvidia-smi", "--query-gpu=index,name,memory.total,compute_cap",
               "--format=csv,noheader,nounits"]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    gpus = []
    for line in result.stdout.splitlines():
        columns = [part.strip() for part in line.split(",")]
        if len(columns) != 4:
            continue
        try:
            gpus.append({"index": int(columns[0]), "name": columns[1],
                         "memory_mib": int(float(columns[2])), "compute_capability": float(columns[3])})
        except (ValueError, OverflowError):
            continue
    return gpus


def profile_gpu_indices(profile: dict | None, available: set[int]) -> list[int]:
    if profile is None:
        return sorted(available)
    selected = sorted({*profile.get("strata_gpu_indices", []),
                       *(index for item in profile.get("services", []) for index in item.get("gpu_indices", []))})
    invalid = [index for index in selected if index not in available]
    if invalid:
        raise ValueError(f"Profile refers to GPU index {invalid[0]}, but that NVIDIA GPU was not found.")
    return selected or sorted(available)


def preflight(args) -> int:
    profile = load_profile(Path(args.profile).expanduser()) if args.profile else None
    gpus = inventory_gpus()
    if not gpus:
        print("Could not read NVIDIA GPU capability with nvidia-smi.")
        return 2
    selected = set(profile_gpu_indices(profile, {gpu["index"] for gpu in gpus}))
    threshold = 6.0 if args.experimental_sm60 else 7.5
    mode = "experimental CUDA 12.x build" if args.experimental_sm60 else "standard Strata build"
    print(f"Strata hardware preflight ({mode}; compute capability {threshold:.1f}+)")
    ok = True
    for gpu in gpus:
        assigned = gpu["index"] in selected
        compatible = gpu["compute_capability"] >= threshold
        state = "ready" if compatible else "unsupported"
        if assigned and not compatible:
            ok = False
        print(f"GPU {gpu['index']}: {gpu['name']} · {gpu['memory_mib'] / 1024:.1f} GiB · "
              f"CC {gpu['compute_capability']:.1f} · {state}{'' if assigned else ' (not selected by profile)'}")
    if not ok:
        if args.experimental_sm60:
            print("This profile includes a GPU below Strata's experimental CC 6.0 floor. Tesla M40 is CC 5.2.")
        else:
            print("A selected GPU is below the standard CC 7.5 floor. Older supported cards need the documented "
                  "experimental build; cards below CC 6.0, including Tesla M40, are unsupported.")
        return 1
    print("The selected GPU hardware meets this Strata build's capability floor.")
    return 0


def systemctl(*args: str) -> subprocess.CompletedProcess:
    if shutil.which("systemctl") is None or os.name == "nt":
        raise RuntimeError("This command needs systemd user services on Linux.")
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=30)


def stack(args) -> int:
    profile = load_profile(Path(args.profile).expanduser())
    units = [item["unit"] for item in profile.get("services", [])] + profile.get("tunnel_units", [])
    if not units:
        print("This profile has no service or tunnel units. Add the installed unit names first.")
        return 2
    if args.action == "stop":
        units.reverse()
    failed = False
    if args.action in ("start", "stop", "enable"):
        for unit in units:
            operation = "enable" if args.action == "enable" else args.action
            result = systemctl(operation, unit)
            if result.returncode:
                failed = True
                detail = (result.stderr or result.stdout).strip()
                print(f"{unit}: {operation} failed{': ' + detail if detail else ''}")
            else:
                print(f"{unit}: {operation} requested")
        if failed:
            return 1
    state_check = "is-enabled" if args.action == "enable" else "is-active"
    for unit in units:
        result = systemctl(state_check, unit)
        state = result.stdout.strip() or "inactive"
        print(f"{unit}: {state}")
        if args.action in ("enable", "status") and result.returncode:
            failed = True
    return 1 if failed else 0


def sanitized_profile(profile: dict) -> dict:
    """Copy only share-safe deployment labels and unit names from an operator profile."""
    services = []
    for item in profile.get("services", []):
        row = {"unit": item["unit"], "role": str(item.get("role") or "")[:80],
               "gpu_indices": list(item.get("gpu_indices", []))}
        services.append(row)
    return {"name": str(profile.get("name") or "Strata deployment")[:80],
            "strata_gpu_indices": list(profile.get("strata_gpu_indices", [])),
            "services": services,
            "tunnel_units": list(profile.get("tunnel_units", [])),
            "gpu_roles_file": "gpu-roles.json" if profile.get("gpu_roles_file") else ""}


def export_bundle(args) -> int:
    profile_path = Path(args.profile).expanduser()
    profile = load_profile(profile_path)
    lifetime_payload = None
    if args.include_lifetime_usage:
        default_state = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
        state_path = Path(args.lifetime_usage_file or os.environ.get("STRATA_LIFETIME_USAGE_FILE") or
                          (default_state / "strata" / "api-usage-lifetime.json")).expanduser()
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("The lifetime usage file has an unsupported format.")
        counts = {key: int(payload.get(key, 0)) for key in ("version", "prompt_tokens", "output_tokens", "requests")}
        counts.update({key: payload.get(key) for key in ("last_event_id", "backfilled_at", "tracking_started_at")})
        if min(counts["prompt_tokens"], counts["output_tokens"], counts["requests"]) < 0:
            raise ValueError("The lifetime usage file has invalid totals.")
        lifetime_payload = counts
    output = Path(args.output).expanduser().resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError(f"Output directory must be new or empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    gpus = inventory_gpus()
    revision = "unknown"
    try:
        result = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True,
                                text=True, timeout=5)
        if result.returncode == 0:
            revision = result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    manifest = {"format_version": 1, "created_at_utc": now, "strata_revision": revision,
                "hardware": gpus, "profile": sanitized_profile(profile),
                "included": ["hardware inventory", "service unit names", "GPU role labels"],
                "excluded": ["API keys", "tunnel credentials", "public tunnel hostnames", "model weights",
                             "prompts", "usage events", "logs", "generated files"]}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (output / "stack-profile.json").write_text(json.dumps(sanitized_profile(profile), indent=2) + "\n",
                                                 encoding="utf-8")
    roles_name = profile.get("gpu_roles_file") or "gpu-roles.json"
    roles_path = Path(roles_name)
    if not roles_path.is_absolute():
        roles_path = (profile_path.parent / roles_path).resolve()
    roles = {}
    if roles_path.is_file():
        try:
            source = json.loads(roles_path.read_text(encoding="utf-8"))
            if isinstance(source, dict):
                roles = {str(index): {"role": str(value.get("role") or "")[:48],
                                      "service": str(value.get("service") or "")[:120]}
                         for index, value in source.items() if str(index).isdigit() and isinstance(value, dict)}
        except (OSError, ValueError):
            roles = {}
    (output / "gpu-roles.json").write_text(json.dumps(roles, indent=2) + "\n", encoding="utf-8")
    (output / ".env.example").write_text(
        "# Copy to a private environment file and set local paths for this machine.\n"
        "# Never commit the private file or any tunnel credentials.\n"
        "STRATA_GPU_ROLES_FILE=./gpu-roles.json\n"
        "STRATA_GENERATOR_USAGE_FILE=\n"
        "STRATA_LIFETIME_USAGE_FILE=\n",
        encoding="utf-8")
    readme = ("# Strata migration bundle\n\n"
              "This bundle is a sanitized inventory and handoff template, not a full backup. It does not contain "
              "models, generated files, prompts, API keys, logs, or tunnel credentials. Review `stack-profile.json`, "
              "update unit names and GPU indices for the destination, install model services and licensed weights, "
              "then recreate tunnels with new credentials.\n\n"
              "Run `python tools/strata_migration.py preflight --profile stack-profile.json --experimental-sm60` "
              "when using the experimental CUDA 12.x path; omit that flag to check the standard build. Run "
              "`python tools/strata_migration.py enable --profile stack-profile.json` to enable boot startup, then "
              "`python tools/strata_migration.py start --profile stack-profile.json` to start the services now.\n")
    (output / "README.md").write_text(readme, encoding="utf-8")
    if lifetime_payload is not None:
        private = output / "private"
        private.mkdir(mode=0o700)
        (private / "api-usage-lifetime.json").write_text(json.dumps(lifetime_payload, indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(private, 0o700)
            os.chmod(private / "api-usage-lifetime.json", 0o600)
        except OSError:
            pass
        print("Included the private lifetime aggregate. Do not share this bundle publicly.")
    print(f"Wrote sanitized migration bundle to {output}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("preflight", help="check NVIDIA compute capability and profile GPU indices")
    check.add_argument("--profile", help="optional service profile JSON")
    check.add_argument("--experimental-sm60", action="store_true",
                       help="check the experimental CUDA 12.x floor of compute capability 6.0")
    check.set_defaults(func=preflight)
    for action in ("enable", "start", "stop", "status"):
        cmd = sub.add_parser(action, help=f"{action} or inspect profile services with systemd --user")
        cmd.add_argument("--profile", default=os.environ.get("STRATA_STACK_PROFILE", str(DEFAULT_PROFILE)),
                         help="service profile JSON (default: Max AI example profile)")
        cmd.set_defaults(func=stack, action=action)
    export = sub.add_parser("export", help="write a sanitized migration handoff bundle")
    export.add_argument("--profile", default=os.environ.get("STRATA_STACK_PROFILE", str(DEFAULT_PROFILE)))
    export.add_argument("--output", required=True, help="new or empty destination directory")
    export.add_argument("--include-lifetime-usage", action="store_true",
                        help="copy only the aggregate token counts into private/; do not share publicly")
    export.add_argument("--lifetime-usage-file", help="private lifetime aggregate path")
    export.set_defaults(func=export_bundle)
    args = parser.parse_args()
    try:
        return args.func(args)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f"migration: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
