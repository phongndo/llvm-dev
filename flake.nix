{
  description = "LLVM monorepo development environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    # nixos-unstable no longer supports Intel macOS.
    nixpkgs-x86_64-darwin.url = "github:NixOS/nixpkgs/nixpkgs-26.05-darwin";
  };

  outputs =
    {
      nixpkgs,
      nixpkgs-x86_64-darwin,
      ...
    }:
    let
      systems = [
        "aarch64-darwin"
        "aarch64-linux"
        "x86_64-darwin"
        "x86_64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      nixpkgsFor = system: if system == "x86_64-darwin" then nixpkgs-x86_64-darwin else nixpkgs;
    in
    {
      formatter = forAllSystems (
        system:
        let
          pkgs = (nixpkgsFor system).legacyPackages.${system};
        in
        pkgs.writeShellApplication {
          name = "format-flake";
          runtimeInputs = [ pkgs.nixfmt ];
          text = ''exec nixfmt "$PWD/flake.nix"'';
        }
      );

      devShells = forAllSystems (
        system:
        let
          pkgs = (nixpkgsFor system).legacyPackages.${system};
          runtimeLibraries = with pkgs; [
            libffi
            libxml2
            ncurses
            zlib
            zstd
          ];
        in
        {
          default = pkgs.mkShell {
            packages = with pkgs; [
              ccache
              cmake
              git
              llvmPackages.clang
              llvmPackages.clang-tools
              llvmPackages.lld
              ninja
              nixd
              pkg-config
              python3
              swig
            ];

            buildInputs = runtimeLibraries;

            CMAKE_C_COMPILER_LAUNCHER = "ccache";
            CMAKE_CXX_COMPILER_LAUNCHER = "ccache";
            CMAKE_EXPORT_COMPILE_COMMANDS = "ON";

            # CMake links bootstrap tools against Nix store libraries without
            # preserving their store paths in the build RPATH. NixOS has no
            # global library search path, so make those tools runnable.
            LD_LIBRARY_PATH = pkgs.lib.optionalString pkgs.stdenv.hostPlatform.isLinux (
              pkgs.lib.makeLibraryPath (runtimeLibraries ++ [ pkgs.stdenv.cc.cc.lib ])
            );

            shellHook = ''
              export CC="${pkgs.llvmPackages.clang}/bin/clang"
              export CXX="${pkgs.llvmPackages.clang}/bin/clang++"

              llvm_dev_root="$PWD"
              while [[ "$llvm_dev_root" != "/" && ! -f "$llvm_dev_root/flake.nix" ]]; do
                llvm_dev_root="''${llvm_dev_root%/*}"
                [[ -n "$llvm_dev_root" ]] || llvm_dev_root="/"
              done

              if [[ -d "$llvm_dev_root/llvm-project" ]]; then
                export LLVM_ROOT="$llvm_dev_root"
                export LLVM_SRC="$LLVM_ROOT/llvm-project"
                export LLVM_BUILD="$LLVM_ROOT/build/dev"
                export PATH="$PATH:$LLVM_BUILD/bin"
              fi
              unset llvm_dev_root
            '';
          };
        }
      );
    };
}
