#!/usr/bin/env python3
"""
LiteLLM endpoint hostname resolver.

Rewrites mDNS (.local) hostnames in a LiteLLM config's api_base URLs to their
resolved IP addresses.  LiteLLM resolves endpoints via aiodns (c-ares), which
does pure DNS and cannot resolve mDNS names; this makes .local endpoints
reachable by pinning the IP that glibc/avahi resolves.

Only http:// URLs whose host ends in .local are rewritten; the port and path
are preserved.  https:// endpoints are left untouched (rewriting the host to an
IP would break SNI/certificate validation), and non-.local names are left for
aiodns to resolve via real DNS.  If a hostname fails to resolve, the entry is
left unchanged so LiteLLM retains the model entry; the next discovery cycle
will retry.

This is invoked by discovery.py after the merged config is written and before
the change-hash is computed, so the final config file always carries IPs and
the hash reflects the IP-based config.

Usage: resolver.py CONFIG_YAML
"""

import socket
import sys
from urllib.parse import urlparse, urlunparse

import yaml


def resolve_api_base(api_base):
    """If api_base is an http:// URL with a .local host, return it with the
    host replaced by the resolved IP (port/path preserved).  Otherwise return
    it unchanged."""
    if not isinstance(api_base, str):
        return api_base
    parsed = urlparse(api_base)
    if parsed.scheme != "http":  # leave https:// (SNI/cert) and others alone
        return api_base
    host = parsed.hostname
    if not host or not host.endswith(".local"):
        return api_base
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET)
        ip = infos[0][4][0]
    except Exception as e:
        print(f"  Warning: could not resolve {host}: {e}", file=sys.stderr)
        return api_base
    # Rebuild netloc preserving port and userinfo (if any).
    netloc = ip
    if parsed.port is not None:
        netloc = f"{ip}:{parsed.port}"
    if parsed.username:
        userinfo = parsed.username
        if parsed.password:
            userinfo += f":{parsed.password}"
        netloc = f"{userinfo}@{netloc}"
    return urlunparse(parsed._replace(netloc=netloc))


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} CONFIG_YAML", file=sys.stderr)
        sys.exit(2)
    path = sys.argv[1]
    with open(path) as f:
        config = yaml.safe_load(f)
    if config is None:
        return
    changed = False
    for entry in config.get("model_list", []):
        if not isinstance(entry, dict):
            continue
        params = entry.get("litellm_params")
        if not isinstance(params, dict):
            continue
        api_base = params.get("api_base")
        resolved = resolve_api_base(api_base)
        if resolved != api_base:
            params["api_base"] = resolved
            changed = True
    if changed:
        with open(path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
        print("  Resolved .local hostnames to IPs.", file=sys.stderr)
    else:
        print("  No .local hostnames to resolve.", file=sys.stderr)


if __name__ == "__main__":
    main()