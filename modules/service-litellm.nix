{
  config,
  lib,
  pkgs,
  options,
  ...
}:
with lib;
let
  cfg = config.cfg.litellm;

  # Whether the agenix module is imported (for optional secrets support).
  hasAgenix = builtins.hasAttr "age" options;

  # Derive a valid age.secrets name from an env var name.
  # e.g. "OPENAI_API_KEY" -> "litellm_openai_api_key"
  secretName = name: "litellm_${toLower name}";

  # Effective secrets: user-provided secrets + master key file (if set).
  # The master key is merged into the same secrets mechanism so it gets
  # the same age decryption, env file generation, and injection treatment.
  effectiveSecrets = cfg.secrets
    // (optionalAttrs (cfg.masterKeyFile != null) {
      LITELLM_MASTER_KEY = cfg.masterKeyFile;
    });

  hasMasterKey = cfg.masterKeyFile != null;

  # Map each secret to its age.secrets path for use in the ExecStartPre script.
  # Guarded by hasAgenix — returns [] if agenix is not imported.
  secretEntries =
    if hasAgenix then
      mapAttrsToList (name: _: {
        inherit name;
        path = config.age.secrets.${secretName name}.path;
      }) effectiveSecrets
    else
      [ ];

  # Script that reads each decrypted age secret and writes a systemd
  # EnvironmentFile to /run/litellm/secrets.env.  Runs as root (+ prefix)
  # so it can read the age secrets (owned by root) and write to the
  # RuntimeDirectory.  The file is mode 0600 (root-only) and is read by
  # systemd (PID 1, also root) before the service starts — the DynamicUser
  # process never reads the file directly, it receives the values as
  # environment variables.
  #
  # Each secret is written as KEY=<value> with a trailing newline.
  # The value is read from the age-decrypted file and written as-is
  # (no shell expansion, no quoting issues).  systemd EnvironmentFile
  # does not support shell substitution — the values must be literal.
  secretsEnvScript = pkgs.writeShellScript "litellm-secrets-env" ''
    set -eu
    : > /run/litellm/secrets.env
    ${concatStringsSep "\n" (map (entry: ''
      printf '${entry.name}=' >> /run/litellm/secrets.env
      cat ${entry.path} >> /run/litellm/secrets.env
      printf '\n' >> /run/litellm/secrets.env
    '') secretEntries)}
    chmod 600 /run/litellm/secrets.env
  '';

  # ── Endpoint expansion ──────────────────────────────────────────────────────
  # Maps provider names to the litellm_params.model prefix used in config.yaml.
  # Special cases use a non-obvious prefix; all others default to
  # "${provider}/".
  providerPrefix = provider:
    if provider == "ollama" then "ollama_chat/"
    else if provider == "openai" then ""
    else "${provider}/";

  # Expands an endpoint definition into a list of model_list entries.
  # Wildcard endpoints generate a single catch-all entry; explicit model lists
  # generate one entry per model.  Both can coexist on the same endpoint.
  expandEndpoint = name: ep:
    let
      prefix = providerPrefix ep.provider;
      info = { id = name; } // ep.model_info;

      mkEntry = model_name: model_value:
        {
          inherit model_name;
          litellm_params = {
            model = model_value;
            api_base = ep.api_base;
            api_key = ep.api_key;
          };
          model_info = info;
        }
        // optionalAttrs (ep.weight != 1) { weight = ep.weight; }
        // optionalAttrs (ep.order != 1) { order = ep.order; };
    in
    (if ep.wildcard != null then
      [ (mkEntry "${ep.wildcard}/*" "${prefix}*") ]
    else [])
    ++ (map (model: mkEntry model "${prefix}${model}") ep.models);

  endpointsModelList = flatten (mapAttrsToList expandEndpoint cfg.endpoints);

  hasSecrets = effectiveSecrets != { };

  # ── Auto-discovery ──────────────────────────────────────────────────────────
  # Filter endpoints with autoDiscover enabled.
  autoDiscoverEndpoints = filterAttrs (_: ep: ep.autoDiscover) cfg.endpoints;
  hasAutoDiscover = autoDiscoverEndpoints != { };

  # Python environment with PyYAML for the discovery script.
  pythonEnv = pkgs.python3.withPackages (ps: [ ps.pyyaml ]);

  # Nix-generated base config (static entries from settings + endpoints).
  # The discovery script reads this and merges in discovered models.
  settingsFormat = pkgs.formats.yaml { };
  baseConfigFile = settingsFormat.generate "litellm-base-config.yaml" (cfg.settings // {
    model_list = cfg.settings.model_list ++ endpointsModelList;
  });

  # Derive the discovery type from the provider.
  #   ollama      → queries /api/tags (Ollama's own format, no auth)
  #   anthropic   → queries /v1/models with x-api-key header
  #   everything  → queries /v1/models with Authorization: Bearer
  discoveryType = provider:
    if provider == "ollama" then "ollama"
    else if provider == "anthropic" then "anthropic"
    else "openai";

  # Resolve os.environ/VAR_NAME references in api_key to the decrypted
  # age secret file path.  Returns null if agenix is not available or
  # the key is not a os.environ/ reference.
  resolveApiKeyPath = api_key:
    let
      m = builtins.match "os.environ/(.+)" api_key;
    in
    if m != null && hasAgenix then
      config.age.secrets.${secretName (builtins.head m)}.path
    else
      null;

  # JSON config passed to the discovery script.  Contains endpoint details
  # and file paths.  Generated by Nix so the script is purely data-driven.
  discoveryConfig = pkgs.writeText "litellm-discovery-config.json" (builtins.toJSON {
    base_config_path = baseConfigFile;
    output_config_path = "/run/litellm/config.yaml";
    hash_path = "/run/litellm/config.hash";
    cache_dir = "/run/litellm/discovery";
    endpoints = mapAttrsToList (name: ep: {
      inherit (ep) name api_base api_key weight order model_info;
      provider_prefix = providerPrefix ep.provider;
      discovery_type = discoveryType ep.provider;
      api_key_path = resolveApiKeyPath ep.api_key;
    }) autoDiscoverEndpoints;
  });

  # When host is "0.0.0.0" (all interfaces), cloudflared should connect via
  # localhost.  Otherwise, use the configured host address.
  localAddress = if cfg.host == "0.0.0.0" then "127.0.0.1" else cfg.host;

in
{
  imports = [ ./options.nix ];

  config = mkIf cfg.enable (mkMerge [
    # ── Assertions ────────────────────────────────────────────────────────────
    {
      assertions = [
        {
          assertion = !hasSecrets || hasAgenix;
          message = ''
            cfg.litellm.secrets (or masterKeyFile) requires the agenix
            module to be imported.  Add agenix to your flake inputs and
            import agenix.nixosModules.default in your system configuration.
          '';
        }
        {
          assertion = !hasAutoDiscover || builtins.hasAttr "litellm" options.services;
          message = ''
            cfg.litellm.endpoints.*.autoDiscover requires the upstream
            services.litellm module from nixpkgs to be available.
          '';
        }
        {
          assertion = cfg.domain == null || hasMasterKey;
          message = ''
            cfg.litellm.masterKeyFile must be set when cfg.litellm.domain
            is set — prevents exposing an unsecured public endpoint.
          '';
        }
      ];
    }

    # ── Age secrets ──────────────────────────────────────────────────────────
    # Declare an age secret for each entry in effectiveSecrets (user secrets +
    # master key file).  Guarded by hasAgenix so the module works without it.
    (mkIf (hasSecrets && hasAgenix) {
      age.secrets = mapAttrs' (name: agePath: {
        name = secretName name;
        value = { file = agePath; };
      }) effectiveSecrets;
    })

    # ── Upstream LiteLLM service ─────────────────────────────────────────────
    # Delegates core configuration (systemd unit, tiktoken cache, sandbox
    # hardening, config.yaml generation, etc.) to the native nixpkgs module.
    {
      services.litellm = {
        enable = true;
        package = cfg.package;
        stateDir = cfg.stateDir;
        host = cfg.host;
        port = cfg.port;
        settings = cfg.settings // {
          model_list = cfg.settings.model_list ++ endpointsModelList;
        } // (optionalAttrs hasMasterKey {
          general_settings = (cfg.settings.general_settings or {}) // {
            master_key = "os.environ/LITELLM_MASTER_KEY";
          };
        });
        environment = cfg.environment;
        openFirewall = cfg.openFirewall;
        # We handle environmentFile ourselves to combine it with age secrets.
        environmentFile = null;
      };
    }

    # ── EnvironmentFile: secrets + user env file ──────────────────────────────
    # Override the upstream module's EnvironmentFile to combine our generated
    # secrets file with any user-provided env file.  mkOverride 500 takes
    # priority over the upstream default (1000) so our combined list wins.
    {
      systemd.services.litellm.serviceConfig.EnvironmentFile = mkOverride 500 (
        (if hasSecrets then [ "/run/litellm/secrets.env" ] else [ ])
        ++ optional (cfg.environmentFile != null) cfg.environmentFile
      );
    }

    # ── ExecStartPre: secrets + auto-discovery ────────────────────────────────
    # Both the secrets script and the discovery script run before LiteLLM
    # starts.  Discovery runs first (mkBefore) since it generates the config
    # file that --config points to.  Secrets run last (mkAfter) since they
    # write the EnvironmentFile that systemd injects.  Upstream's tiktoken/
    # UI scripts run between them at default priority.
    #
    # When both conditions are true:  discovery → tiktoken/UI → secrets
    # When only secrets:                         tiktoken/UI → secrets
    # When only discovery:    discovery → tiktoken/UI
    # When neither:            tiktoken/UI only
    {
      systemd.services.litellm.serviceConfig.ExecStartPre = mkMerge [
        (mkIf hasAutoDiscover (mkBefore [
          "+${pythonEnv}/bin/python3 ${./discovery.py} ${discoveryConfig}"
        ]))
        (mkIf hasSecrets (mkAfter [
          "+${secretsEnvScript}"
        ]))
      ];
    }

    # ── Auto-discovery: use runtime config ────────────────────────────────────
    # Override the upstream ExecStart to point at the merged config in /run/
    # instead of the Nix store path.  mkOverride 500 takes priority over the
    # upstream default (1000).  Only applied when autoDiscover is enabled —
    # without it, the upstream config path is correct.
    (mkIf hasAutoDiscover {
      systemd.services.litellm.serviceConfig.ExecStart =
        mkOverride 500 "${lib.getExe cfg.package} --host \"${cfg.host}\" --port ${toString cfg.port} --config /run/litellm/config.yaml";

      # Graceful shutdown timeout: give in-flight requests up to 60 seconds
      # to complete before SIGKILL.  uvicorn handles SIGTERM by draining
      # connections, so most requests finish well within this window.
      systemd.services.litellm.serviceConfig.TimeoutStopSec =
        mkOverride 500 "60";
    })

    # ── Auto-discovery: discovery service ─────────────────────────────────────
    # Periodically queries autoDiscover endpoints, merges discovered models
    # into the config, and restarts LiteLLM only when the model list has
    # actually changed (hash comparison).
    (mkIf hasAutoDiscover {
      systemd.services.litellm-discovery = {
        description = "Discover LiteLLM models from LLM endpoints";
        wantedBy = [ "multi-user.target" ];
        after = [ "network.target" ];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = false;
          ExecStart = "${pythonEnv}/bin/python3 ${./discovery.py} --restart-if-changed ${discoveryConfig}";
          PrivateTmp = true;
        };
        path = [ pkgs.systemd ];
      };
    })

    # ── Auto-discovery: discovery timer ────────────────────────────────────────
    # Runs the discovery service on a configurable interval (default 5min).
    # Disabled when autoDiscoverInterval is null.
    (mkIf (hasAutoDiscover && cfg.autoDiscoverInterval != null) {
      systemd.timers.litellm-discovery = {
        description = "Periodically discover LiteLLM models from LLM endpoints";
        wantedBy = [ "timers.target" ];
        timerConfig = {
          OnBootSec = "30s";
          OnUnitActiveSec = cfg.autoDiscoverInterval;
          AccuracySec = "10s";
        };
      };
    })

    # ── Cloudflared ingress ──────────────────────────────────────────────────
    # Direct to LiteLLM when cloudflared is enabled and a public domain is configured.
    # Uses the configured host address, falling back to localhost when the
    # service binds to all interfaces (0.0.0.0).
    (mkIf (config.services.cloudflared.enable && cfg.domain != null) {
      services.cloudflared = {
        tunnels."${config.networking.hostName}".ingress."${cfg.domain}" =
          "http://${localAddress}:${toString cfg.port}";
      };
    })
  ]);
}