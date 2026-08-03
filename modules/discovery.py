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
import socket
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, urlunparse

import yaml


def resolved_url(api_base):
    """For http://*.local URLs, resolve the host to an IP once (glibc/avahi)
    and return a URL with the IP, so repeated HTTP calls (e.g. one /api/show
    per model) don't each re-resolve via mDNS — which both slows discovery and
    can saturate avahi.  Non-.local URLs are returned unchanged.  The original
    api_base is still written to the config (resolver.py rewrites it for
    LiteLLM); this only affects the discovery script's own HTTP calls.
    """
    try:
        p = urlparse(api_base)
    except Exception:
        return api_base
    if p.scheme != "http" or not (p.hostname or "").endswith(".local"):
        return api_base
    try:
        ip = socket.getaddrinfo(p.hostname, None, socket.AF_INET)[0][4][0]
    except Exception:
        return api_base  # let the HTTP call fail with the original name
    netloc = ip + (f":{p.port}" if p.port else "")
    return urlunparse(p._replace(netloc=netloc))


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


def show_ollama(api_base, model, api_key=None, timeout=10):
    """Query Ollama's /api/show for a single model to get its real context
    window and capabilities.

    Returns (context_length, capabilities) where context_length is an int or
    None, and capabilities is a list of LM Studio-style capability strings
    (e.g. ["tool_use", "vision"]).  On failure returns (None, []).
    """
    try:
        url = f"{api_base.rstrip('/')}/api/show"
        req = urllib.request.Request(
            url,
            data=json.dumps({"model": model}).encode(),
            headers={"Content-Type": "application/json"},
        )
        # Local Ollama needs no auth (its default api_key is "ollama"); only
        # send a real key for authenticated endpoints (e.g. Ollama Cloud).
        if api_key and api_key != "ollama":
            req.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        # The context length lives at <architecture>.context_length (e.g.
        # llama.context_length, nemotron-3-nano.context_length) — find any key
        # ending in .context_length.
        context = None
        for k, v in (data.get("model_info") or {}).items():
            if k.endswith(".context_length") and isinstance(v, int):
                context = v
                break
        caps = []
        for c in data.get("capabilities") or []:
            if c == "tools":
                caps.append("tool_use")
            elif c == "vision":
                caps.append("vision")
        return context, caps
    except Exception as e:
        print(f"    could not fetch /api/show for {model}: {e}", file=sys.stderr)
        return None, []


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
    prefix_discovered = config.get("prefix_discovered_models", False)
    # Optionally fetch per-model context + capabilities via /api/show (Ollama)
    # so the LM Studio adapter can report real per-model values to the editor
    # instead of a one-size-fits-all default.
    fetch_model_info = config.get("fetch_model_info", False)
    model_info_path = config.get("model_info_path")
    model_info_map = {}
    for ep in endpoints:
        name = ep["name"]
        api_base = ep["api_base"]
        # Resolve http://*.local hosts once (glibc/avahi) and use the IP for
        # this endpoint's HTTP calls so repeated /api/show requests don't each
        # re-resolve via mDNS.  The original api_base is still written to the
        # config (resolver.py rewrites it for LiteLLM).
        connect_base = resolved_url(api_base)
        api_key = resolve_api_key(ep.get("api_key"), ep.get("api_key_path"))
        discovery_type = ep.get("discovery_type", "ollama")
        print(f"  Discovering {name} ({api_base}, {discovery_type})...",
              file=sys.stderr)

        if discovery_type == "ollama":
            models = discover_ollama(connect_base)
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
        to_show = []  # (model, model_name) to query via /api/show
        for model in models:
            # Prefix the model_name with the endpoint name so the selector
            # shows which endpoint a model routes to (e.g.
            # `ollama_cloud:deepseek-v4-flash`).  The litellm_params.model
            # (the backend model) is left untouched.
            model_name = f"{name}:{model}" if prefix_discovered else model
            key = (model_name, api_base)
            if key in existing:
                continue
            existing.add(key)

            entry = {
                "model_name": model_name,
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

            # Collect Ollama models for per-model /api/show (context + caps).
            if fetch_model_info and discovery_type == "ollama":
                to_show.append((model, model_name))

        # Fetch per-model context + capabilities via /api/show in parallel
        # (5 at a time) so a large model list doesn't make discovery slow.
        if to_show:
            with ThreadPoolExecutor(max_workers=5) as ex:
                futs = {
                    ex.submit(show_ollama, connect_base, model, api_key): model_name
                    for model, model_name in to_show
                }
                for fut in as_completed(futs):
                    model_name = futs[fut]
                    try:
                        ctx, caps = fut.result()
                    except Exception as e:
                        print(f"    /api/show failed: {e}", file=sys.stderr)
                        continue
                    if ctx is not None or caps:
                        model_info_map[model_name] = {
                            "context": ctx,
                            "capabilities": caps,
                        }

    # ── Write the per-model info sidecar (read by the LM Studio adapter) ──────
    if fetch_model_info and model_info_path:
        os.makedirs(os.path.dirname(model_info_path), exist_ok=True)
        with open(model_info_path, "w") as f:
            json.dump(model_info_map, f)
        print(f"  Wrote per-model info for {len(model_info_map)} models "
              f"to {model_info_path}", file=sys.stderr)

    # ── Write merged config (carrying .local domain names from the base) ──────
    merged_yaml = yaml.dump(base_config, default_flow_style=False, sort_keys=False)
    os.makedirs(os.path.dirname(output_config_path), exist_ok=True)
    with open(output_config_path, "w") as f:
        f.write(merged_yaml)

    # ── Resolve .local hostnames to IPs ──────────────────────────────────────
    # LiteLLM resolves endpoints via aiodns (c-ares), which does pure DNS and
    # cannot resolve mDNS (.local) names.  Rewrite http://*.local api_base
    # hostnames to their resolved IPs (via glibc, which uses avahi) so LiteLLM
    # can reach them.  This runs AFTER the merged config is written (which still
    # carries the domain names from the base config) and BEFORE the hash, so the
    # final file always contains IPs and the hash reflects the IP-based config
    # (so a DHCP IP change is detected and triggers a restart).  The resolver is
    # a separate script (resolver.py) for separation of concerns.
    resolver_path = config.get("resolver_path")
    if resolver_path:
        try:
            subprocess.run(
                [sys.executable, resolver_path, output_config_path],
                check=True, timeout=30,
            )
        except subprocess.CalledProcessError as e:
            print(f"  Warning: hostname resolver failed: {e}", file=sys.stderr)
        except subprocess.TimeoutExpired:
            print("  Warning: hostname resolver timed out", file=sys.stderr)

    # ── Hash the final (resolved) config; restart litellm only if changed ────
    with open(output_config_path) as f:
        final_yaml = f.read()
    new_hash = sha256_of(final_yaml)

    if os.path.exists(hash_path):
        with open(hash_path) as f:
            old_hash = f.read().strip()
        if new_hash == old_hash:
            print(f"No changes detected ({discovered_count} discovered models, "
                  f"config unchanged).", file=sys.stderr)
            sys.exit(0)

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