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
                                 rewrite; text streams through unchanged,
                                 tool-call chunks are reshaped on the fly to
                                 fix LiteLLM's Ollama streaming bug where
                                 `function.arguments` arrives as a JSON object
                                 instead of a string).
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


# LM Studio parameter names that differ from OpenAI's.  The adapter receives
# requests in LM Studio format (via /api/v0/chat/completions) and forwards
# them to LiteLLM's OpenAI-compatible /v1/chat/completions.  Without
# translation, LM Studio-specific parameters pass through to the provider
# and cause errors — e.g. maxOutputTokens:-1 (LM Studio's sentinel for "use
# model default") reaches Vertex AI as-is and is rejected.
LMSTUDIO_PARAM_MAP = {
    "maxOutputTokens": "max_tokens",
}
# Sentinel values that mean "not set / use model default" in LM Studio.
# These are stripped so the provider uses its own default.
SENTINEL_VALUES = {-1, 0, None}


def translate_lmstudio_body(body_bytes):
    """Translate LM Studio chat-completion parameters to OpenAI format.

    Returns bytes (possibly the original bytes unchanged if no translation
    was needed or the body isn't valid JSON).
    """
    if not body_bytes:
        return body_bytes
    try:
        body = json.loads(body_bytes)
    except (json.JSONDecodeError, ValueError):
        return body_bytes
    changed = False
    for lm_name, oai_name in LMSTUDIO_PARAM_MAP.items():
        if lm_name in body:
            val = body.pop(lm_name)
            if val not in SENTINEL_VALUES and oai_name not in body:
                body[oai_name] = val
            changed = True
    if not changed:
        return body_bytes
    return json.dumps(body).encode()


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


class ChatStreamRewriter:
    """Stateful rewriter for OpenAI chat-completion SSE chunks.

    Fixes two LiteLLM/Ollama streaming bugs that break strict clients like Zed's
    LM Studio provider:

    1. `function.arguments` arrives as a JSON *object* (Ollama returns a dict)
       instead of a JSON *string*.  Zed's `FunctionChunk.arguments` is
       `Option<String>`, so an object makes the whole SSE chunk fail to
       deserialize.  (The non-streaming path is fine — LiteLLM `transform_response`
       explicitly `json.dumps`s the args.)

    2. The tool call arrives in its own chunk with *no* `finish_reason`, and the
       trailing `done` chunk carries Ollama's `done_reason: "stop"`.  LiteLLM's
       `chunk_parser` only overrides the finish reason to `"tool_calls"` when the
       tool calls and `done: true` are in the *same* chunk, so here the stream ends
       with `finish_reason: "stop"`.  Zed only emits a tool-use event on
       `finish_reason == "tool_calls"`; with `"stop"` it ends the turn and the
       accumulated tool call is silently dropped — exactly the symptom of "the
       agent stops instead of making the tool call".

    We track whether any `delta.tool_calls` was seen in the stream and, when the
    final chunk arrives with a non-`tool_calls` finish reason, rewrite it to
    `"tool_calls"`.  We also backfill `index` (Zed requires it, no serde default)
    and `type` defensively.
    """

    def __init__(self):
        self.saw_tool_calls = False
        self.finish_emitted = False

    def process(self, line):
        """Convert one upstream SSE line into bytes to write downstream.

        Only `data:` payloads are reshaped, and only re-serialized when they were
        actually mutated; everything else passes through verbatim.  Returns None
        for blank separator lines (we re-emit our own `\n\n` framing per line).
        """
        s = line.rstrip(b"\r")
        if not s:
            return None
        if s.startswith(b"data: "):
            payload = s[6:]
        elif s.startswith(b"data:"):
            payload = s[5:]
        else:
            # comments / event lines etc. — clients ignore them; preserve as-is
            return s + b"\n\n"
        if payload.strip() == b"[DONE]":
            return b"data: [DONE]\n\n"
        try:
            obj = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return b"data: " + payload + b"\n\n"
        if self._rewrite(obj):
            payload = json.dumps(obj).encode()
        return b"data: " + payload + b"\n\n"

    def _rewrite(self, obj):
        """Mutate `obj` in place; return True if it changed."""
        changed = False
        for choice in obj.get("choices") or []:
            delta = choice.get("delta") or {}
            tcs = delta.get("tool_calls")
            if tcs:
                self.saw_tool_calls = True
                for tc in tcs:
                    if not isinstance(tc, dict):
                        continue
                    if "index" not in tc:
                        tc["index"] = 0
                        changed = True
                    if not tc.get("type"):
                        tc["type"] = "function"
                        changed = True
                    fn = tc.get("function")
                    if not isinstance(fn, dict):
                        fn = {}
                        tc["function"] = fn
                        changed = True
                    args = fn.get("arguments")
                    if args is not None and not isinstance(args, str):
                        # Ollama sends a dict (or sometimes a list); OpenAI wants
                        # a JSON string the client reassembles/appends per chunk.
                        fn["arguments"] = json.dumps(args)
                        changed = True
            # Rewrite the trailing finish reason: if a tool call was streamed
            # earlier but the final chunk says "stop" (Ollama's done_reason),
            # force it to "tool_calls" so the client fires the tool-use event.
            # Only act on the first finish reason we see, to avoid emitting a
            # second "tool_calls" if the provider already set one.
            fr = choice.get("finish_reason")
            if fr and not self.finish_emitted:
                self.finish_emitted = True
                if self.saw_tool_calls and fr != "tool_calls":
                    choice["finish_reason"] = "tool_calls"
                    changed = True
        return changed


async def chat_completions(req):
    """LM Studio chat = OpenAI chat, with the path rewritten to /v1/chat/completions.

    Text chunks stream through unchanged.  Tool-call chunks are reshaped on the
    fly to fix LiteLLM's Ollama streaming bugs (arguments object → string, and
    trailing `finish_reason: "stop"` → `"tool_calls"`); see `ChatStreamRewriter`.
    Non-SSE responses (e.g. upstream errors, or a client that set stream=false)
    are passed through byte-for-byte.
    """
    upstream = req.app["upstream"]
    target = upstream + "/v1/chat/completions"
    if req.query_string:
        target += "?" + req.query_string
    body = await req.read() if req.body_exists else None
    if body:
        body = translate_lmstudio_body(body)
    headers = client_headers(req)
    timeout = ClientTimeout(total=None)
    async with ClientSession(timeout=timeout) as session:
        async with session.post(target, data=body, headers=headers) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            is_sse = "text/event-stream" in ctype
            resp_headers = {
                k: v for k, v in r.headers.items()
                if k.lower() not in HOP_BY_HOP and k.lower() != "content-length"
            }
            resp = web.StreamResponse(status=r.status, headers=resp_headers)
            if is_sse:
                resp.content_type = "text/event-stream"
            await resp.prepare(req)
            if not is_sse:
                # Not a stream (error body, or stream=false): forward verbatim.
                async for chunk in r.content.iter_any():
                    await resp.write(chunk)
                await resp.write_eof()
                return resp
            rewriter = ChatStreamRewriter()
            buf = b""
            async for chunk in r.content.iter_any():
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    out = rewriter.process(line)
                    if out is not None:
                        await resp.write(out)
            if buf.strip():
                out = rewriter.process(buf)
                if out is not None:
                    await resp.write(out)
            await resp.write_eof()
            return resp


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
