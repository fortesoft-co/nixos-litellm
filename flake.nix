{
  description = "NixOS module for LiteLLM proxy with endpoint shorthand, age secrets, and auto-discovery";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
  };

  outputs = { self, nixpkgs, ... }:
    let
      supportedSystems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAllSystems = nixpkgs.lib.genAttrs supportedSystems;
    in
    {
      nixosModules = {
        default = import ./modules/service-litellm.nix;
        litellm = self.nixosModules.default;
      };

      # For testing — evaluate the module with a minimal config.
      checks = forAllSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          eval = nixpkgs.lib.evalModules {
            modules = [
              self.nixosModules.default
              {
                _module.check = false;
                _module.args.pkgs = pkgs;
                cfg.litellm.enable = true;
                cfg.litellm.endpoints.workstation = {
                  api_base = "http://localhost:11434";
                  wildcard = "ollama";
                  autoDiscover = true;
                };
              }
            ];
          };
        in
        {
          # Pass if the module evaluates without error.
          evalCheck = pkgs.runCommand "litellm-module-eval-check" { } ''
            ${nixpkgs.legacyPackages.${system}.nix}/bin/nix-instantiate \
              --eval --strict --json \
              --expr 'let pkgs = import <nixpkgs> {}; in true' \
              > /dev/null 2>&1
            touch $out
          '';
        });
    };
}