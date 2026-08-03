#!/usr/bin/env python3
"""
LiteLLM LM Studio-compatible adapter.

Sits in front of a LiteLLM proxy (OpenAI-compatible) and makes it also speak
the subset of LM Studio's API that an editor's `lmstudio` provider uses, so
the editor can auto-discover models (they typically cannot enumerate custom
OpenAI-compatible providers).

Routes:
  GET  /api/v0/models          → LiteLLM GET /v1/models, reshaped to LM
                                 Studio's {data:[{id, object, type, publisher,
                                 compatibility_type, state, ...}]} format.
  POST /api/v0/chat/completions → LiteLLM POST /v1/chat/completions (path
                                 rewrite only; LM Studio chat is OpenAI chat,
                                 so the body and streaming pass through).
  *    /*                       → forwarded to LiteLLM unchanged (so /v1/*,
                                 /health, the LiteLLM UI, etc. all work).

Auth is end-to-end: the client's Authorization header (editor's LMSTUDIO_API_KEY)
is forwarded to LiteLLM, which enforces it. The adapter holds no secrets.

Usage: lmstudio_adapter.py --listen HOST --port PORT --upstream http://host:port
"""

import argparse
import json
from aiohttp import web, ClientSession, ClientTimeout

# Hop-by-hop / length headers that the proxy must not copy verbatim — aiohttp
# manages these per-connection.
HOP_BY_HOP = {
    "host", "content-length", "transfer-encoding", "connection",
    "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailers", "upgrade",
}


def client_headers(req):
    return {k: v for k, v in req.headers.items() if k.lower() not in HOP_BY_HOP}


def response_headers(resp):
    return {k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP}


async def stream_proxy(req, upstream, *, path=None):
    """Forward `req` to `{upstream}{path or req.path}` and stream the response
    back unchanged.  Used for the catch-all passthrough and for the LM Studio
    chat route (with path rewritten to /v1/chat/completions)."""
    target = upstream + (path if path is not None else req.path)
    if req.query_string:
        target += "?" + req.query_string
    body = await req.read() if req.body_exists else None
    headers = client_headers(req)
    timeout = ClientTimeout(total=None)  # chat completions can stream for a while
    async with ClientSession(timeout=timeout) as session:
        async with session.request(req.method, target, data=body, headers=headers) as r:
            stream = web.StreamResponse(status=r.status, headers=response_headers(r))
            await stream.prepare(req)
            async for chunk in r.content.iter_any():
                await stream.write(chunk)
            await stream.write_eof()
            return stream


def load_model_info(path):
    """Load the per-model info sidecar written by discovery.py.

    Returns a dict mapping model_name -> {"context": int|None, "capabilities":
    [str]}.  Returns {} if the file is missing/unreadable (the adapter then
    falls back to the configured defaults for every model).
    """
    if not path:
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def caps_from_model_info(mi):
    """Derive LM Studio capability strings from a LiteLLM /model/info
    `model_info` sub-dict (the registry metadata for known models)."""
    caps = []
    if mi.get("supports_function_calling"):
        caps.append("tool_use")
    if mi.get("supports_vision"):
        caps.append("vision")
    return caps


async def get_models(req):
    """Reshape LiteLLM's OpenAI /v1/models into LM Studio's /api/v0/models."""
    upstream = req.app["upstream"]
    headers = client_headers(req)
    async with ClientSession(timeout=ClientTimeout(total=30)) as session:
        async with session.get(f"{upstream}/v1/models", headers=headers) as r:
            text = await r.text()
            if r.status != 200:
                return web.Response(
                    status=r.status, text=text,
                    content_type=r.headers.get("Content-Type", "text/plain"),
                )
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                return web.Response(status=502, text="upstream returned non-JSON")

        # LiteLLM's /model/info carries registry metadata (max_tokens,
        # supports_function_calling, supports_vision) for *known* models
        # (OpenAI, Anthropic, Gemini, …).  It's a fallback for context /
        # capabilities when the /api/show sidecar (Ollama) has nothing — so
        # cloud models routed through LiteLLM also get real per-model values.
        # Returns None for Ollama/custom models not in LiteLLM's registry.
        model_info = {}
        try:
            async with session.get(f"{upstream}/model/info", headers=headers) as r:
                if r.status == 200:
                    mi = json.loads(await r.text())
                    for e in mi.get("data", []):
                        name = e.get("model_name")
                        if name:
                            model_info[name] = e.get("model_info") or {}
        except Exception:
            pass

    # Per-model context + capabilities, with precedence:
    #   1. /api/show sidecar (Ollama, written by discovery.py)
    #   2. LiteLLM /model/info registry (known cloud models)
    #   3. configured defaults (defaultContext / capabilities)
    sidecar = load_model_info(req.app["model_info_path"])
    default_context = req.app["default_context"]
    default_capabilities = req.app["capabilities"]
    out = []
    for m in data.get("data", []):
        mid = m.get("id", "")
        sc = sidecar.get(mid)
        mi = model_info.get(mid, {})
        if sc is not None:
            # /api/show entry exists — use its caps verbatim (even [] if the
            # model has no tools), only filling missing context from the registry.
            context = sc.get("context") or mi.get("max_tokens") or mi.get("max_input_tokens")
            caps = sc.get("capabilities", [])
            if caps is None:
                caps = caps_from_model_info(mi)
        else:
            context = mi.get("max_tokens") or mi.get("max_input_tokens")
            caps = caps_from_model_info(mi)
        # Final fallback to defaults only when nothing provided a value.
        if not context:
            context = default_context
        if not caps and sc is None:
            caps = default_capabilities
        out.append({
            "id": mid,
            "object": "model",
            "type": "llm",
            "publisher": "litellm",
            "arch": None,
            "compatibility_type": "gguf",
            "quantization": None,
            "state": "not-loaded",
            "max_context_length": context,
            "loaded_context_length": None,
            "capabilities": caps if caps else [],
        })
    return web.json_response({"data": out})


async def chat_completions(req):
    """LM Studio chat = OpenAI chat; rewrite the path and pass through."""
    return await stream_proxy(req, req.app["upstream"], path="/v1/chat/completions")


async def catch_all(req):
    """Everything else (e.g. /v1/*, /health, the LiteLLM UI) passes through."""
    return await stream_proxy(req, req.app["upstream"])


async def health(_req):
    return web.Response(text="ok")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--listen", required=True)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--upstream", required=True)
    p.add_argument("--default-context", type=int, default=32768,
                   help="context window reported to editor for every model")
    p.add_argument("--capabilities", default="tool_use",
                   help="comma-separated LM Studio capabilities, e.g. tool_use,vision")
    p.add_argument("--model-info", default=None,
                   help="path to the per-model info sidecar from discovery.py")
    args = p.parse_args()

    app = web.Application()
    app["upstream"] = args.upstream.rstrip("/")
    app["default_context"] = args.default_context
    app["capabilities"] = [c.strip() for c in args.capabilities.split(",") if c.strip()]
    app["model_info_path"] = args.model_info
    # Explicit LM Studio routes first (static routes take priority over the
    # catch-all dynamic route below).
    app.router.add_get("/health", health)
    app.router.add_get("/api/v0/models", get_models)
    app.router.add_post("/api/v0/chat/completions", chat_completions)
    app.router.add_route("*", "/{tail:.*}", catch_all)

    web.run_app(app, host=args.listen, port=args.port)


if __name__ == "__main__":
    main()
