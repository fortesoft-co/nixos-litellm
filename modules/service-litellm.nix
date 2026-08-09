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

  # Runtime location of the generated environment file.  This lives in a
  # *separate* RuntimeDirectory (litellm-secrets, owned by the secrets
  # oneshot) rather than litellm's own RuntimeDirectory, because systemd
  # loads EnvironmentFile *before* litellm's ExecStartPre runs — so the file
  # must already exist by the time litellm starts.  The litellm-secrets
  # oneshot (see below) generates it first, with RemainAfterExit so the
  # directory persists for litellm's lifetime.
  secretsRuntimeDir = "/run/litellm-secrets";
  secretsEnvFile = "${secretsRuntimeDir}/secrets.env";

  # JSON secrets are copied to this subdirectory and the env var is set to
  # the file path instead of the file contents.  This avoids systemd's
  # EnvironmentFile backslash escape handling corrupting multi-line JSON
  # (e.g. GCP service account keys with \n in the private key).  The
  # directory is chowned to the litellm DynamicUser at service start (see
  # chownJsonSecretsScript below) so files are not world-readable.
  jsonSecretsDir = "${secretsRuntimeDir}/json";

  # Map each secret to its age.secrets path for use in the secrets oneshot.
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
  # EnvironmentFile to ${secretsEnvFile}.  Runs as root (the litellm-secrets
  # oneshot has no DynamicUser) so it can read the age secrets (owned by
  # root) and write to its own RuntimeDirectory.  The file is mode 0600
  # (root-only) and is read by systemd (PID 1, also root) when litellm
  # starts — the DynamicUser litellm process never reads the file directly,
  # it receives the values as environment variables.
  #
  # For each secret, the script detects whether the decrypted content is
  # JSON (via jq -e .).  JSON secrets are copied to ${jsonSecretsDir}/ and
  # the env var is set to the file path — consumers like LiteLLM's
  # vertex_credentials check os.path.exists() and read the file.  Non-JSON
  # secrets are written as KEY=<value> as before.  This avoids systemd's
  # EnvironmentFile backslash escape handling corrupting multi-line JSON.
  secretsEnvScript = pkgs.writeShellScript "litellm-secrets-env" ''
    set -eu
    : > ${secretsEnvFile}
    mkdir -p ${jsonSecretsDir}
    ${concatStringsSep "\n" (map (entry: ''
      if ${pkgs.jq}/bin/jq -e . ${entry.path} >/dev/null 2>&1; then
        cp ${entry.path} ${jsonSecretsDir}/${entry.name}.json
        chmod 600 ${jsonSecretsDir}/${entry.name}.json
        printf '${entry.name}=${jsonSecretsDir}/${entry.name}.json\n' >> ${secretsEnvFile}
      else
        printf '${entry.name}=' >> ${secretsEnvFile}
        cat ${entry.path} >> ${secretsEnvFile}
        printf '\n' >> ${secretsEnvFile}
      fi
    '') secretEntries)}
    chmod 600 ${secretsEnvFile}
  '';

  # Runs as root (via ExecStartPre + prefix) before litellm starts.  chowns
  # the JSON secrets directory to the DynamicUser's UID/GID so only the
  # litellm process can read the files.  The DynamicUser's UID is obtained
  # from /run/litellm/ (the litellm RuntimeDirectory, which systemd creates
  # and chowns to the DynamicUser before ExecStartPre runs).
  chownJsonSecretsScript = pkgs.writeShellScript "litellm-chown-json-secrets" ''
    set -eu
    uid=$(${pkgs.coreutils}/bin/stat -c %u /run/litellm)
    gid=$(${pkgs.coreutils}/bin/stat -c %g /run/litellm)
    ${pkgs.coreutils}/bin/chown -R "$uid:$gid" ${jsonSecretsDir}
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

      # Ollama endpoints ignore api_key but LiteLLM historically requires one
      # to be present.  Auto-fill "ollama" so users don't have to set it.
      effectiveApiKey =
        if ep.api_key != null then ep.api_key
        else if ep.provider == "ollama" then "ollama"
        else null;

      mkEntry = model_name: model_value:
        {
          inherit model_name;
          litellm_params = { model = model_value; }
            // optionalAttrs (ep.api_base != null) { api_base = ep.api_base; }
            // optionalAttrs (effectiveApiKey != null) { api_key = effectiveApiKey; }
            // ep.litellm_params;
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
    if api_key == null then null
    else if m != null && hasAgenix then
      config.age.secrets.${secretName (builtins.head m)}.path
    else
      null;

  # JSON config passed to the discovery script.  Contains endpoint details
  # and file paths.  Generated by Nix so the script is purely data-driven.
  #
  # The config is written under /run/litellm-discovery/ (created by the
  # discovery script via makedirs, not a systemd RuntimeDirectory) so its
  # lifetime is decoupled from litellm's own RuntimeDirectory — litellm can
  # read it at boot even though litellm-discovery runs `before=litellm`.
  # The file is written 0644 so litellm's DynamicUser can read it.
  discoveryConfig = pkgs.writeText "litellm-discovery-config.json" (builtins.toJSON {
    base_config_path = baseConfigFile;
    output_config_path = "/run/litellm-discovery/config.yaml";
    hash_path = "/run/litellm-discovery/config.hash";
    cache_dir = "/run/litellm-discovery/cache";
    # Prefix auto-discovered model_names with the endpoint name so the
    # model selector shows which endpoint a model routes to.
    prefix_discovered_models = cfg.prefixDiscoveredModels;
    # Separate script that rewrites http://*.local api_base hostnames to their
    # resolved IPs (glibc/avahi), since LiteLLM resolves via aiodns (no mDNS).
    # Invoked by discovery.py after writing the merged config and before hashing.
    resolver_path = ./resolver.py;
    endpoints = mapAttrsToList (name: ep: {
      inherit name;
      inherit (ep) api_base api_key weight order model_info litellm_params;
      provider_prefix = providerPrefix ep.provider;
      discovery_type = discoveryType ep.provider;
      api_key_path = resolveApiKeyPath ep.api_key;
    }) autoDiscoverEndpoints;
  });

  # When host is "0.0.0.0" (all interfaces), cloudflared should connect via
  # localhost.  Otherwise, use the configured host address.
  localAddress = if cfg.host == "0.0.0.0" then "127.0.0.1" else cfg.host;

  # Override the litellm package to use the fix/ollama-model-info-capabilities
  # fork.  The fork uses maturin as the build backend, but nixpkgs' litellm
  # package is set up for uv-build; patch pyproject.toml to match.  The Rust
  # bridge is not built (uv-build skips it), but litellm falls back to Python
  # implementations when the native extension is absent.
  litellmPackage = pkgs.litellm.overrideAttrs (old: {
    version = "1.97.0-fork";
    src = pkgs.fetchFromGitHub {
      owner = "fortesoftware";
      repo = "litellm";
      rev = "2b89bdeed1";
      hash = "sha256-WAHjObD2AIE3U6NenZ/ttpWsgdFkPFz6c24wm0Pcww0=";
    };
    postPatch = ''
      substituteInPlace pyproject.toml \
        --replace-fail 'requires = ["maturin==1.9.4"]' 'requires = ["uv_build"]' \
        --replace-fail 'build-backend = "maturin"' 'build-backend = "uv_build"'
    '';
  });

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
          assertion = !hasAutoDiscover || all (ep: ep.api_base != null) (attrValues autoDiscoverEndpoints);
          message = ''
            cfg.litellm.endpoints.*.autoDiscover requires api_base to be set
            (the discovery script needs a URL to query).  Set api_base or
            disable autoDiscover for that endpoint.
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
        package = litellmPackage;
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
    # litellm's EnvironmentFile is loaded by systemd *before* its ExecStartPre
    # runs, so the secrets file must already exist at start time.  It is
    # therefore generated by the separate litellm-secrets oneshot (see below)
    # into its own RuntimeDirectory (/run/litellm-secrets), not by a litellm
    # ExecStartPre.  The upstream module sets EnvironmentFile with a plain
    # assignment (priority 100), so we must use mkForce (priority 50) to win.
    # A user-provided cfg.environmentFile is a static, pre-existing file and
    # can stay in the list as-is.
    {
      systemd.services.litellm.serviceConfig.EnvironmentFile = mkForce (
        (if hasSecrets then [ secretsEnvFile ] else [ ])
        ++ optional (cfg.environmentFile != null) cfg.environmentFile
      );
    }

    # ── Secrets oneshot ──────────────────────────────────────────────────────
    # Generates ${secretsEnvFile} from age-decrypted secrets before litellm
    # starts.  Runs as root (no DynamicUser) so it can read /run/agenix/*.
    # RemainAfterExit keeps its RuntimeDirectory alive for litellm's lifetime
    # so the EnvironmentFile remains readable.  agenix decrypts during the
    # sysinit phase (run-agenix.d.mount), and regular services start after
    # sysinit, so the secrets are available by the time this oneshot runs.
    (mkIf (hasSecrets && hasAgenix) {
      systemd.services.litellm-secrets = {
        description = "Generate LiteLLM secrets environment file";
        before = [ "litellm.service" ];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          RuntimeDirectory = "litellm-secrets";
          RuntimeDirectoryMode = "0755";
          ExecStart = "${secretsEnvScript}";
        };
      };

      # Pull and order the oneshot around litellm: start it when litellm
      # starts, wait for it, and fail litellm if it fails.
      systemd.services.litellm = {
        requires = [ "litellm-secrets.service" ];
        after = [ "litellm-secrets.service" ];
      };

      # Chown JSON secrets directory to the DynamicUser so only litellm can
      # read them.  Runs as root (via + prefix) before litellm starts.  The
      # DynamicUser's UID/GID is obtained from /run/litellm/ (the litellm
      # RuntimeDirectory, which systemd creates and chowns to the DynamicUser
      # before ExecStartPre runs).
      systemd.services.litellm.serviceConfig.ExecStartPre =
        mkBefore [ "+${chownJsonSecretsScript}" ];
    })

    # ── Auto-discovery: litellm depends on the discovery service ──────────────
    # Discovery (+ hostname resolution) is handled by the litellm-discovery
    # oneshot below, which runs `before=litellm` at boot and on the periodic timer.
    # litellm therefore reads the generated /run/litellm-discovery/config.yaml.
    # Upstream's tiktoken/UI ExecStartPre scripts are left in place.
    (mkIf hasAutoDiscover {
      systemd.services.litellm.after = [ "litellm-discovery.service" ];
    })

    # ── Auto-discovery: use runtime config ────────────────────────────────────
    # Override the upstream ExecStart to point at the merged config in
    # /run/litellm-discovery/ instead of the Nix store path. The upstream
    # module sets ExecStart with a plain assignment (priority 100), so we use
    # mkForce (priority 50) to win.  Only applied when autoDiscover is
    # enabled — without it, the upstream config path is correct.
    (mkIf hasAutoDiscover {
      systemd.services.litellm.serviceConfig.ExecStart =
        mkForce "${lib.getExe litellmPackage} --host \"${cfg.host}\" --port ${toString cfg.port} --config /run/litellm-discovery/config.yaml";

      # Graceful shutdown timeout: give in-flight requests up to 60 seconds
      # to complete before SIGKILL.  uvicorn handles SIGTERM by draining
      # connections, so most requests finish well within this window.
      systemd.services.litellm.serviceConfig.TimeoutStopSec =
        mkForce "60";
    })

    # ── Auto-discovery: discovery + resolver service ───────────────────────────
    # Runs discovery.py (merge discovered models) then resolver.py (rewrite
    # .local hostnames to IPs) then hashes the final config and restarts
    # litellm only when it actually changed.  Runs at boot (wantedBy
    # multi-user, before=litellm so litellm waits for the config) and on the
    # periodic timer.  RemainAfterExit stays false so the timer re-runs
    # ExecStart each cycle (with true, `systemctl start` from the timer would
    # be a no-op on an already-active unit).  Runs as root (no DynamicUser) so
    # it can read age secrets for authenticated endpoints and resolve mDNS.
    (mkIf hasAutoDiscover {
      systemd.services.litellm-discovery = {
        description = "Discover LiteLLM models and resolve endpoint hostnames";
        wantedBy = [ "multi-user.target" ];
        before = [ "litellm.service" ];
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
    # Directs the public domain at cfg.port. Uses the configured host
    # address, falling back to localhost when the service binds to all
    # interfaces (0.0.0.0).
    (mkIf (config.services.cloudflared.enable && cfg.domain != null) {
      services.cloudflared = {
        tunnels."${config.networking.hostName}".ingress."${cfg.domain}" =
          "http://${localAddress}:${toString cfg.port}";
      };
    })

    # Open the LiteLLM port for LAN access.
    (mkIf (cfg.openFirewall) {
      networking.firewall.allowedTCPPorts = [ cfg.port ];
    })
  ]);
}
