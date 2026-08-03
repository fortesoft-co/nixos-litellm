{
  config,
  lib,
  pkgs,
  ...
}:
with lib;
let
  settingsFormat = pkgs.formats.yaml { };
in
{
  options.cfg.litellm = {
    enable = mkEnableOption "LiteLLM proxy server";

    package = mkPackageOption pkgs "litellm" { };

    stateDir = mkOption {
      type = types.path;
      default = "/var/lib/litellm";
      description = ''
        State directory for LiteLLM.
        Stores UI assets and the tiktoken cache.
        Set to a persistent path (e.g. on /Volumes/Server) to survive
        reimaging — the default /var/lib/litellm is on the root filesystem.
      '';
    };

    host = mkOption {
      type = types.str;
      default = "127.0.0.1";
      description = ''
        The host address which the LiteLLM server HTTP interface listens to.
        Use "0.0.0.0" to allow remote access.
      '';
    };

    port = mkOption {
      type = types.port;
      default = 8080;
      description = ''
        Which port the LiteLLM server listens on.
      '';
    };

    settings = mkOption {
      type = types.submodule {
        freeformType = settingsFormat.type;
        options = {
          model_list = mkOption {
            type = settingsFormat.type;
            description = ''
              List of supported models on the server, with model-specific configs.
              Use os.environ/VAR_NAME to reference secrets from the secrets option:

                  cfg.litellm.settings.model_list = [{
                    model_name = "gpt-4o";
                    litellm_params.model = "gpt-4o";
                    litellm_params.api_key = "os.environ/OPENAI_API_KEY";
                  }];
                  cfg.litellm.secrets.OPENAI_API_KEY = ./secrets/openai.age;

              For common providers, cfg.litellm.endpoints provides a shorthand
              that generates model_list entries automatically from endpoint
              definitions.
            '';
            default = [ ];
          };

          router_settings = mkOption {
            type = settingsFormat.type;
            description = ''
              LiteLLM Router settings.
            '';
            default = { };
          };

          litellm_settings = mkOption {
            type = settingsFormat.type;
            description = ''
              LiteLLM Module settings.
            '';
            default = { };
          };

          general_settings = mkOption {
            type = settingsFormat.type;
            description = ''
              LiteLLM Server settings.
            '';
            default = { };
          };

          environment_variables = mkOption {
            type = settingsFormat.type;
            description = ''
              Environment variables to pass to the LiteLLM proxy.
            '';
            default = { };
          };
        };
      };
      default = { };
      description = ''
        Configuration for LiteLLM, written to config.yaml at service start.
        See <https://docs.litellm.ai/docs/proxy/configs> for available options.

        Secrets should never be placed directly in this option — they would end
        up in the world-readable Nix store.  Instead, reference them with
        os.environ/VAR_NAME and declare the variable in cfg.litellm.secrets.
      '';
    };

    endpoints = mkOption {
      type = types.attrsOf (types.submodule {
        options = {
          api_base = mkOption {
            type = types.str;
            description = ''
              Base URL for the LLM endpoint (e.g. "http://workstation.local:11434"
              or "https://api.openai.com/v1").
            '';
          };

          api_key = mkOption {
            type = types.str;
            default = "ollama";
            description = ''
              API key for the endpoint.  Use "os.environ/VAR_NAME" to reference
              a secret declared in cfg.litellm.secrets.  Local Ollama endpoints
              ignore this value but LiteLLM requires it to be set.
            '';
          };

          provider = mkOption {
            type = types.str;
            default = "ollama";
            description = ''
              LLM provider.  Determines the model prefix used in
              litellm_params.model.  Common values and their prefixes:

                ollama       → ollama_chat/<model>  (Ollama /api/chat endpoint)
                openai       → <model>               (no prefix)
                azure        → azure/<model>
                anthropic    → anthropic/<model>
                vertex_ai    → vertex_ai/<model>
                bedrock      → bedrock/<model>
                gemini       → gemini/<model>
                mistral      → mistral/<model>
                huggingface  → huggingface/<model>
                cohere       → cohere/<model>
                deepseek     → deepseek/<model>
                groq         → groq/<model>
                together_ai  → together_ai/<model>

              Any unlisted provider uses <provider>/<model> as the prefix.
              See https://docs.litellm.ai/docs/providers for the full list.
            '';
          };

          models = mkOption {
            type = types.listOf types.str;
            default = [ ];
            description = ''
              List of model names available at this endpoint.  Each name generates
              a model_list entry that appears in LiteLLM's /v1/models endpoint.
            '';
          };

          wildcard = mkOption {
            type = types.nullOr types.str;
            default = null;
            description = ''
              When set, generates a catch-all model_list entry that forwards any
              unmatched model name to this endpoint.  The value is used as the
              model_name prefix: e.g. "ollama" produces model_name "ollama/*".
              Endpoints sharing the same wildcard name are load-balanced (use
              weight to control distribution).
            '';
          };

          autoDiscover = mkOption {
            type = types.bool;
            default = false;
            description = ''
                When true, query this endpoint at service start and periodically
                to discover available models.  Discovered models are added as
                explicit model_list entries alongside any wildcard or static
                models, making them visible in LiteLLM's /v1/models endpoint.

                Discovery type is derived from the provider field:

                  ollama     → GET /api/tags (no auth required)
                  anthropic  → GET /v1/models with x-api-key header
                  everything → GET /v1/models with Authorization: Bearer

                The last category covers all OpenAI-compatible providers:
                OpenAI, Groq, Together AI, Deepseek, Mistral, Fireworks AI,
                Perplexity, xAI, OpenRouter, and local servers like vLLM,
                LM Studio, and Llamafile.

                Providers that require SDK-specific auth (Vertex AI, Bedrock,
                Sagemaker, WatsonX) are not supported for discovery — use
                models with manual listing instead.

                If discovery fails (e.g. endpoint unreachable), the last
                cached model list is used.  If no cache exists, the endpoint
                is skipped.

                The discovery service runs on a timer
                (cfg.litellm.autoDiscoverInterval) and only restarts LiteLLM
                when the model list actually changes (hash comparison),
                avoiding unnecessary restarts.
              '';
          };

          weight = mkOption {
            type = types.int;
            default = 1;
            description = ''
              Load-balancing weight.  When multiple endpoints share the same
              model_name, LiteLLM distributes requests proportionally.
            '';
          };

          order = mkOption {
            type = types.int;
            default = 1;
            description = ''
              Fallback priority.  Lower values are tried first; higher values
              serve as fallbacks when all lower-order deployments fail.
            '';
          };

          model_info = mkOption {
            type = types.attrs;
            default = { };
            description = ''
              Additional model_info metadata attached to each generated model_list
              entry.  The id field is auto-set to the endpoint name unless overridden.
            '';
          };
        };
      });
      default = { };
      description = ''
        LLM endpoints to route to through the LiteLLM proxy.  Each endpoint
        generates model_list entries for the LiteLLM config, reducing the
        boilerplate of specifying api_base, api_key, and provider prefix for
        every individual model.

        Endpoints with the same model_name or wildcard name are load-balanced
        according to their weight.  Use order to set fallback priority.

        Example:

            cfg.litellm.endpoints = {
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
      '';
    };

    autoDiscoverInterval = mkOption {
      type = types.nullOr types.str;
      default = "5min";
      example = "1h";
      description = ''
        How often to re-discover models from autoDiscover endpoints.
        Set to null to disable periodic re-discovery (models are only
        discovered at service start).

        Accepts systemd timer OnUnitActiveSec values (e.g. "5min", "1h",
        "30min").  Discovery only triggers a LiteLLM restart when the
        model list has actually changed (hash comparison).
      '';
    };

    prefixDiscoveredModels = mkOption {
      type = types.bool;
      default = false;
      description = ''
        Prefix each auto-discovered model's `model_name` with the endpoint
        name (e.g. `workstation:llama3.2`, `ollama_cloud:deepseek-v4-flash`),
        so the model selector shows which endpoint a model routes to.  This
        matters when several endpoints expose models with similar names.

        Only affects auto-discovered entries; wildcard catch-alls (e.g.
        `ollama/*`) and explicitly-listed models keep their existing
        `model_name`.  Clients must request the prefixed name for discovered
        models (the wildcard still catches unprefixed `ollama/<model>`).  Off
        by default to preserve the raw discovered model names for existing
        setups.
      '';
    };

    environment = mkOption {
      type = types.attrsOf types.str;
      default = {
        SCARF_NO_ANALYTICS = "True";
        DO_NOT_TRACK = "True";
        ANONYMIZED_TELEMETRY = "False";
      };
      description = ''
        Extra environment variables for the LiteLLM service.
        Values here are written to the Nix store — do NOT put secrets here.
        Use cfg.litellm.secrets instead.
      '';
    };

    environmentFile = mkOption {
      type = types.nullOr types.path;
      default = null;
      description = ''
        Environment file to be passed to the systemd service.
        Useful for passing non-secret env vars from a file.
        For secrets, prefer cfg.litellm.secrets which decrypts age-encrypted
        files at runtime and never writes them to the Nix store.
      '';
    };

    secrets = mkOption {
      type = types.attrsOf types.path;
      default = { };
      example = literalExpression ''
        {
          OPENAI_API_KEY = ./secrets/openai-api-key.age;
          ANTHROPIC_API_KEY = ./secrets/anthropic-api-key.age;
        }
      '';
      description = ''
        Age-encrypted secrets to inject as environment variables.

        Each attribute maps an environment variable name to an age-encrypted
        file path.  At service start, each secret is decrypted by agenix and
        written to a root-owned file on tmpfs (/run).  The values are never
        stored in the Nix store.

        Reference these in cfg.litellm.settings with LiteLLM's
        os.environ/VAR_NAME syntax:

            cfg.litellm.settings.model_list = [{
              model_name = "gpt-4o";
              litellm_params.model = "gpt-4o";
              litellm_params.api_key = "os.environ/OPENAI_API_KEY";
            }];
            cfg.litellm.secrets.OPENAI_API_KEY = ./secrets/openai.age;
      '';
    };

    openFirewall = mkOption {
      type = types.bool;
      default = false;
      description = ''
        Whether to open the firewall for LiteLLM.
        This adds cfg.litellm.port to networking.firewall.allowedTCPPorts.
      '';
    };

    masterKeyFile = mkOption {
      type = types.nullOr types.path;
      default = null;
      example = "./secrets/litellm-master-key.age";
      description = ''
        Age-encrypted file containing the LiteLLM proxy master key.

        When set, the module declares the age secret, injects it as the
        LITELLM_MASTER_KEY environment variable, and sets
        general_settings.master_key in the LiteLLM config to
        os.environ/LITELLM_MASTER_KEY.  All requests to the proxy will
        require Authorization: Bearer <key>.

        The key must start with "sk-" (LiteLLM requirement).

        Required when cfg.litellm.domain is set — prevents exposing an
        unsecured public endpoint.  Optional for local-only use.
      '';
    };

    domain = mkOption {
      type = types.nullOr types.str;
      default = null;
      description = ''
        Public domain for the LiteLLM proxy (e.g. litellm.example.com).
        When set, adds a cloudflared ingress entry — requires the
        service-cloudflared module to be imported on this host.
        Leave null for local-only access (use host + openFirewall instead).

        Requires cfg.litellm.masterKeyFile to be set — prevents exposing
        an unsecured public endpoint.
      '';
    };

    lmstudioAdapter = mkOption {
      type = types.submodule {
        options = {
          enable = mkEnableOption ''
            an LM Studio-compatible API adapter in front of LiteLLM.  LiteLLM
            speaks only the OpenAI API, which editors like Zed cannot enumerate
            through their custom OpenAI-compatible provider (Zed requires a
            manual model list there).  Zed does, however, auto-discover models
            for its native `lmstudio` provider via LM Studio's
            `/api/v0/models` endpoint.

            When enabled, this adapter takes over cfg.litellm.port as the
            public-facing port (the one cloudflared/firewall already target)
            and LiteLLM is moved to an internal port (cfg.litellm.port + 1,
            bound to 127.0.0.1 only).  No separate domain, cloudflared entry,
            or firewall rule is needed — the existing LiteLLM endpoint now
            also speaks the LM Studio API.

            The adapter reshapes LiteLLM's /v1/models into LM Studio's
            /api/v0/models format, rewrites /api/v0/chat/completions to
            LiteLLM's /v1/chat/completions, and passes every other request
            (e.g. /v1/*, /health, the LiteLLM UI) straight through.  LM Studio
            chat is OpenAI chat, so chat needs no translation (including
            streaming).

            Authentication is end-to-end: the client's Authorization header
            (Zed's LMSTUDIO_API_KEY, set to the LiteLLM master key) is
            forwarded to LiteLLM, which enforces it.  The adapter holds no
            secrets.
          '';

          defaultContext = mkOption {
            type = types.ints.unsigned;
            default = 32768;
            description = ''
              Context window (in tokens) reported to Zed for every discovered
              model.  LiteLLM's OpenAI-shaped /v1/models does not expose context
              sizes, and Zed's `lmstudio` provider defaults to 2048 when none is
              reported — so the adapter reports this value for all models.

              Set this to roughly what your backends actually serve (e.g. match
              Ollama's `num_ctx`) so Zed doesn't over- or under-fill the context.
              Override per-model in Zed's `lmstudio.available_models` if you need
              accuracy for specific models.
            '';
          };

          capabilities = mkOption {
            type = types.listOf types.str;
            default = [ "tool_use" ];
            example = [ "tool_use" "vision" ];
            description = ''
              LM Studio capability strings reported to Zed for every discovered
              model.  LiteLLM's /v1/models does not expose capabilities, so the
              adapter reports these defaults.  `tool_use` enables tool calling
              in Zed; `vision` enables image input.  Only include `vision` if
              your backends actually accept images, otherwise Zed will offer
              image attachments that the model rejects.
            '';
          };

          fetchModelInfo = mkOption {
            type = types.bool;
            default = true;
            description = ''
              During discovery, query each Ollama model's `/api/show` to fetch
              its real context window and tool/vision capabilities, and report
              those per-model to Zed (via a sidecar file the LM Studio adapter
              reads) instead of the one-size-fits-all `defaultContext`/
              `capabilities`.  Models without info (non-Ollama endpoints, or
              `/api/show` failures) fall back to the defaults.

              Adds one `/api/show` request per discovered Ollama model per
              discovery cycle.  Only takes effect when the LM Studio adapter
              is enabled.
            '';
          };
        };
      };
      default = { };
      description = ''
        LM Studio-compatible adapter that makes LiteLLM also speak the LM
        Studio API, so editors (e.g. Zed) can auto-discover models via their
        native `lmstudio` provider instead of maintaining a manual model list.
        Sits in front of LiteLLM on cfg.litellm.port.
      '';
    };
  };
}
