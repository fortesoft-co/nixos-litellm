# nixos-litellm

A NixOS module that wraps the upstream [`services.litellm`](https://github.com/NixOS/nixpkgs/blob/master/nixos/modules/services/misc/litellm.nix) module from nixpkgs with:

- **Endpoint shorthand** — define an LLM endpoint once with its models, and the module expands it into `model_list` entries automatically
- **Age secrets** — pass API keys via agenix-encrypted files; secrets are decrypted at runtime and never written to the Nix store
- **Master key** — secure the proxy with a LiteLLM master key; required when exposing publicly
- **Auto-discovery** — query Ollama `/api/tags` or OpenAI-compatible `/v1/models` endpoints to automatically enumerate available models
- **Cloudflared ingress** — optional public domain routing through a Cloudflare tunnel

## Usage

Add to your flake inputs:

```nix
inputs.nixos-litellm.url = "github:fortesoft-co/nixos-litellm";
```

Import the module:

```nix
{
  inputs,
  ...
}:
{
  imports = [ inputs.nixos-litellm.nixosModules.default ];
}
```

Configure:

```nix
cfg.litellm = {
  enable = true;
  host = "0.0.0.0";
  port = 4000;
  domain = "litellm.example.com";

  # Required when domain is set — secures the public endpoint.
  # Generate: openssl rand -hex 24 | sed 's/^/sk-/'
  masterKeyFile = ./secrets/litellm-master-key.age;

  endpoints = {
    workstation = {
      api_base = "http://workstation.local:11434";
      wildcard = "ollama";
      autoDiscover = true;
      weight = 2;
    };
    cloud = {
      api_base = "https://api.ollama.com/v1";
      api_key = "os.environ/OLLAMA_API_KEY";
      models = [ "llama3.1" "mistral" ];
      order = 2;
    };
  };

  secrets = {
    OLLAMA_API_KEY = ./secrets/litellm_ollama_api_key.age;
  };
};
```

## Options

### `cfg.litellm.enable`
Enable the LiteLLM proxy server.

### `cfg.litellm.package`
The LiteLLM package to use (defaults to `pkgs.litellm`).

### `cfg.litellm.host` / `cfg.litellm.port`
Bind address and port for the HTTP server.

### `cfg.litellm.stateDir`
State directory for UI assets and tiktoken cache.

### `cfg.litellm.settings`
Full LiteLLM config in Nix attribute sets, serialized to YAML. See the [LiteLLM config docs](https://docs.litellm.ai/docs/proxy/configs). For secrets, use `os.environ/VAR_NAME` and declare them in `secrets`.

### `cfg.litellm.endpoints`
Attribute set of LLM endpoints. Each endpoint generates `model_list` entries from its `models` and/or `wildcard`. Key sub-options:

- `api_base` — Base URL for the endpoint
- `api_key` — API key (use `os.environ/VAR_NAME` for secrets)
- `provider` — LLM provider (`ollama`, `openai`, `anthropic`, `vertex_ai`, etc.)
- `models` — List of model names (appear in `/v1/models`)
- `wildcard` — Catch-all model name prefix (e.g. `"ollama"` → `ollama/*`)
- `autoDiscover` — Query the endpoint for available models at start and periodically
- `weight` / `order` — Load-balancing weight and fallback priority

### `cfg.litellm.autoDiscoverInterval`
How often to re-discover models (systemd timer interval, default `"5min"`). Set to `null` to disable periodic re-discovery.

### `cfg.litellm.masterKeyFile`
Age-encrypted file containing the LiteLLM proxy master key. When set, the module:
- Declares the age secret and decrypts it at runtime
- Injects it as the `LITELLM_MASTER_KEY` environment variable
- Sets `general_settings.master_key = "os.environ/LITELLM_MASTER_KEY"` in the config

All requests to the proxy will require `Authorization: Bearer <key>`. The key must start with `sk-` (LiteLLM requirement).

**Required when `domain` is set** — prevents exposing an unsecured public endpoint. Optional for local-only use.

### `cfg.litellm.secrets`
Attribute set mapping environment variable names to age-encrypted file paths. Requires the [agenix](https://github.com/ryantm/agenix) module to be imported.

### `cfg.litellm.environment` / `cfg.litellm.environmentFile`
Extra environment variables (non-secret) and optional environment file.

### `cfg.litellm.openFirewall`
Whether to open the firewall for the LiteLLM port.

### `cfg.litellm.domain`
Public domain for Cloudflare tunnel ingress. Requires `services.cloudflared` to be enabled. When set, `masterKeyFile` must also be set to secure the public endpoint.

## Auto-discovery

When `autoDiscover = true` on an endpoint, the module queries the endpoint at service start and periodically (via a systemd timer) to discover available models.

Discovery type is derived from the `provider` field:

| Provider | Discovery endpoint | Auth |
|---|---|---|
| `ollama` | `GET /api/tags` | None |
| `anthropic` | `GET /v1/models` | `x-api-key` header |
| All others | `GET /v1/models` | `Authorization: Bearer` |

The "all others" category covers all OpenAI-compatible providers: OpenAI, Groq, Together AI, Deepseek, Mistral, Fireworks AI, Perplexity, xAI, OpenRouter, and local servers like vLLM, LM Studio, and Llamafile.

Providers that require SDK-specific auth (Vertex AI, Bedrock, Sagemaker, WatsonX) are not supported for auto-discovery — use `models` with manual listing instead.

Discovered models are merged into the config and LiteLLM is restarted only when the model list has actually changed (SHA-256 hash comparison). If an endpoint is unreachable, the last cached model list is used as a fallback.

## Security

LiteLLM supports a master key that gates all requests to the proxy. When `masterKeyFile` is set, every API call must include `Authorization: Bearer <key>`.

For public deployments (with `domain` set), the master key is **required** — the module asserts this to prevent accidentally exposing an unsecured endpoint. The key is stored in an age-encrypted file, decrypted at runtime to tmpfs, and injected as an environment variable — it never touches the Nix store.

For local-only deployments, `masterKeyFile` is optional but recommended. Without it, any process on the network can access the proxy without authentication.