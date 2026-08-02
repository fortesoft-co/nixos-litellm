#!/usr/bin/env python3
"""
LiteLLM Model Auto-Discovery

Queries LLM endpoints for available models, merges discovered entries
into the base config, and restarts LiteLLM only when the config has
actually changed (hash comparison).

Supports two discovery formats:
  - Ollama:          GET /api/tags      → models[].name
  - OpenAI-compatible: GET /v1/models   → data[].id

For authenticated endpoints, pass api_key in the config.  For
os.environ/ references, pass the decrypted secret file path via
api_key_path — the script reads the value directly from disk.

Usage:
  litellm-discovery [--restart-if-changed] CONFIG_JSON

Options:
  --restart-if-changed  If the config changed, restart litellm.service
                        via systemctl try-restart --no-block.  Without
                        this flag, the script only writes the config file.
"""

import hashlib
import json
import os
import subprocess
import sys
import urllib.request

import yaml


def discover_ollama(api_base, timeout=10):
    """Query Ollama's /api/tags endpoint.

    Returns a list of model names on success, or None on failure.
    """
    try:
        url = f"{api_base.rstrip('/')}/api/tags"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            return [m["name"] for m in data.get("models", [])]
    except Exception as e:
        print(f"  Warning: Ollama discovery failed for {api_base}: {e}",
              file=sys.stderr)
        return None


def discover_openai(api_base, api_key=None, auth_type="bearer", timeout=10):
    """Query an OpenAI-compatible /v1/models endpoint.

    Handles api_base values that already include /v1 (most providers do):
      "https://api.openai.com/v1"       → ".../v1/models"
      "https://api.anthropic.com"       → ".../v1/models"

    Auth types:
      "bearer"  — Authorization: Bearer <key>  (OpenAI, Groq, Together, etc.)
      "x-api-key" — x-api-key: <key>            (Anthropic)

    Returns a list of model IDs on success, or None on failure.
    """
    try:
        base = api_base.rstrip('/')
        if base.endswith('/v1'):
            url = f"{base}/models"
        else:
            url = f"{base}/v1/models"
        req = urllib.request.Request(url)
        if api_key:
            if auth_type == "x-api-key":
                req.add_header("x-api-key", api_key)
                req.add_header("anthropic-version", "2023-06-01")
            else:
                req.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            return [m["id"] for m in data.get("data", [])]
    except Exception as e:
        print(f"  Warning: OpenAI discovery failed for {api_base}: {e}",
              file=sys.stderr)
        return None


def resolve_api_key(api_key, api_key_path):
    """Resolve an API key value.

    If api_key starts with 'os.environ/', reads the value from
    api_key_path (a decrypted age secret file).  Otherwise returns
    api_key as-is.

    Returns the resolved key string, or None if resolution fails.
    """
    if api_key and api_key.startswith("os.environ/"):
        if api_key_path is None:
            print(f"  Warning: cannot resolve {api_key} — no secret path provided",
                  file=sys.stderr)
            return None
        try:
            with open(api_key_path) as f:
                return f.read().strip()
        except Exception as e:
            print(f"  Warning: failed to read secret from {api_key_path}: {e}",
                  file=sys.stderr)
            return None
    return api_key


def load_cache(cache_dir, name):
    """Load a cached model list from disk, or None if no cache exists."""
    path = os.path.join(cache_dir, f"{name}.json")
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def save_cache(cache_dir, name, models):
    """Persist a model list to the cache directory."""
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"{name}.json")
    with open(path, "w") as f:
        json.dump(models, f)


def sha256_of(content):
    """Return the SHA-256 hex digest of a string."""
    return hashlib.sha256(content.encode()).hexdigest()


def main():
    restart_if_changed = "--restart-if-changed" in sys.argv
    config_json_path = None
    for arg in sys.argv[1:]:
        if not arg.startswith("--"):
            config_json_path = arg

    if config_json_path is None:
        print(f"Usage: {sys.argv[0]} [--restart-if-changed] CONFIG_JSON",
              file=sys.stderr)
        sys.exit(2)

    # ── Read discovery configuration ──────────────────────────────────────────
    with open(config_json_path) as f:
        config = json.load(f)

    base_config_path = config["base_config_path"]
    output_config_path = config["output_config_path"]
    hash_path = config["hash_path"]
    cache_dir = config["cache_dir"]
    endpoints = config["endpoints"]

    # ── Read the Nix-generated base config ───────────────────────────────────
    with open(base_config_path) as f:
        base_config = yaml.safe_load(f)

    if base_config is None:
        base_config = {}
    if "model_list" not in base_config:
        base_config["model_list"] = []

    # ── Build a set of existing (model_name, api_base) pairs for dedup ────────
    existing = set()
    for entry in base_config.get("model_list", []):
        if isinstance(entry, dict) and "model_name" in entry:
            api_base = entry.get("litellm_params", {}).get("api_base", "")
            existing.add((entry["model_name"], api_base))

    # ── Discover models for each endpoint ─────────────────────────────────────
    discovered_count = 0
    for ep in endpoints:
        name = ep["name"]
        api_base = ep["api_base"]
        api_key = resolve_api_key(ep.get("api_key"), ep.get("api_key_path"))
        discovery_type = ep.get("discovery_type", "ollama")
        print(f"  Discovering {name} ({api_base}, {discovery_type})...",
              file=sys.stderr)

        if discovery_type == "ollama":
            models = discover_ollama(api_base)
        elif discovery_type == "anthropic":
            models = discover_openai(api_base, api_key=api_key, auth_type="x-api-key")
        elif discovery_type == "openai":
            models = discover_openai(api_base, api_key=api_key, auth_type="bearer")
        else:
            print(f"  Skipping {name}: unknown discovery type '{discovery_type}'",
                  file=sys.stderr)
            continue

        if models is None:
            # Network or HTTP failure — fall back to cache
            models = load_cache(cache_dir, name)
            if models is None:
                print(f"  Skipping {name}: no data and no cache",
                      file=sys.stderr)
                continue
            print(f"  {name}: using cached data ({len(models)} models)",
                  file=sys.stderr)
        else:
            save_cache(cache_dir, name, models)
            print(f"  {name}: discovered {len(models)} models",
                  file=sys.stderr)

        # ── Add discovered models to config ──────────────────────────────────
        for model in models:
            key = (model, api_base)
            if key in existing:
                continue
            existing.add(key)

            entry = {
                "model_name": model,
                "litellm_params": {
                    "model": f"{ep['provider_prefix']}{model}",
                    "api_base": api_base,
                    "api_key": ep.get("resolved_api_key") or api_key or "ollama",
                },
                "model_info": ep.get("model_info", {}),
            }
            if ep.get("weight", 1) != 1:
                entry["weight"] = ep["weight"]
            if ep.get("order", 1) != 1:
                entry["order"] = ep["order"]
            base_config["model_list"].append(entry)
            discovered_count += 1

    # ── Generate merged YAML and compare hash ────────────────────────────────
    merged_yaml = yaml.dump(base_config, default_flow_style=False, sort_keys=False)
    new_hash = sha256_of(merged_yaml)

    if os.path.exists(hash_path) and os.path.exists(output_config_path):
        with open(hash_path) as f:
            old_hash = f.read().strip()
        if new_hash == old_hash:
            print(f"No changes detected ({discovered_count} discovered models, "
                  f"config unchanged).", file=sys.stderr)
            sys.exit(0)

    # ── Write merged config and hash ─────────────────────────────────────────
    os.makedirs(os.path.dirname(output_config_path), exist_ok=True)
    with open(output_config_path, "w") as f:
        f.write(merged_yaml)
    with open(hash_path, "w") as f:
        f.write(new_hash)

    print(f"Config updated ({discovered_count} discovered models).",
          file=sys.stderr)

    # ── Restart litellm if requested ─────────────────────────────────────────
    # try-restart only restarts the service if it is currently active.
    # --no-block returns immediately without waiting for the restart.
    # TimeoutStopSec=60 on the litellm unit gives in-flight requests
    # up to 60 seconds to complete before SIGKILL.
    if restart_if_changed:
        print("Restarting litellm.service (config changed)...", file=sys.stderr)
        try:
            subprocess.run(
                ["systemctl", "try-restart", "--no-block", "litellm.service"],
                check=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            print("Warning: restart command timed out", file=sys.stderr)
        except subprocess.CalledProcessError as e:
            print(f"Warning: restart command failed: {e}", file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()