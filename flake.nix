{
  description = "LLVM development tools";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  inputs.alive2-mcp = {
    url = "github:phongndo/alive2-mcp/v0.1.0";
    inputs.nixpkgs.follows = "nixpkgs";
  };
  inputs.alive2-src = {
    url = "github:AliveToolkit/alive2/c5773898e4e40097b377fe9200ccb82447f4289d";
    flake = false;
  };

  outputs =
    {
      nixpkgs,
      alive2-mcp,
      alive2-src,
      ...
    }:
    let
      systems = nixpkgs.lib.genAttrs [
        "aarch64-darwin"
        "x86_64-linux"
      ];
    in
    {
      formatter = systems (system: nixpkgs.legacyPackages.${system}.nixfmt);
      devShells = systems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          inherit (pkgs) lib;
          linux = pkgs.stdenv.hostPlatform.isLinux;
          # The shell's own compiler, so Linux has no GCC wrapper competing for CC.
          stdenv = pkgs.llvmPackages.stdenv;
          clang = stdenv.cc;
          # Query only the pinned compiler wrappers for Nix's implicit SDK/headers.
          clangd = pkgs.writeShellApplication {
            name = "clangd";
            text = ''
              exec ${pkgs.llvmPackages.clang-tools}/bin/clangd \
                --query-driver=${clang}/bin/clang,${clang}/bin/clang++ "$@"
            '';
          };
          alive2 = pkgs.applyPatches {
            name = "alive2-source-${alive2-src.shortRev}";
            src = alive2-src;
            # Immutable source has no Git metadata; keep third-party warnings nonfatal.
            postPatch = ''
              substituteInPlace CMakeLists.txt \
                --replace-fail 'find_package(Git REQUIRED)' "" \
                --replace-fail 'COMMAND "''${GIT_EXECUTABLE}" describe --tags --dirty --always' \
                  'COMMAND "''${CMAKE_COMMAND}" -E echo "${alive2-src.rev}"' \
                --replace-fail '-Wall -Werror -fPIC' '-Wall -fPIC'
            '';
          };
          # nixpkgs' libpfm release lacks Zen5 PMU support needed by llvm-exegesis.
          libpfm = pkgs.libpfm.overrideAttrs {
            version = "4.13.0-unstable-91970fe";
            src = pkgs.fetchFromGitHub {
              owner = "wcohen";
              repo = "libpfm4";
              rev = "91970fe6eb4e80b63f77fb54a9592e28a207050c";
              hash = "sha256-Uhy1HhKl8WeA4uTfGSPu26FNWIgEYlQk0UeTB/7LpGk=";
            };
            patches = [ ];
            postPatch = "";
          };
          libraries =
            with pkgs;
            [
              libffi
              libxml2
              ncurses
              zlib
              zstd
              z3
            ]
            ++ lib.optionals linux [ libpfm ];
        in
        {
          default = pkgs.mkShell.override { inherit stdenv; } {
            packages =
              with pkgs;
              [
                alive2-mcp.packages.${system}.default
                bash
                ccache
                cmake
                coreutils
                creduce
                fd
                gh
                git
                graphviz
                hyperfine
                jq
                just
                (lib.hiPrio clangd)
                llvmPackages.clang-tools
                llvmPackages.lld
                ninja
                nixd
                nixfmt
                pkg-config
                (python3.withPackages (p: [
                  p.psutil
                  p.pyyaml
                ]))
                ripgrep
                z3
              ]
              ++ lib.optionals linux [
                llvmPackages.lldb
                perf
              ];
            buildInputs = libraries;
            # Baked into Linux build RPATHs by `just configure`. Not LD_LIBRARY_PATH:
            # that would leak this pin's libstdc++ into every program in the shell.
            LLVM_DEV_RUNTIME_LIBRARY_PATH = lib.optionalString linux (
              lib.makeLibraryPath (libraries ++ [ pkgs.stdenv.cc.cc.lib ])
            );
            LLVM_DEV_CLANGD = "${clangd}/bin/clangd";
            ALIVE2_SOURCE = "${alive2}";
            Z3_INCLUDE_DIR = "${lib.getDev pkgs.z3}/include";
            Z3_LIBRARY = "${lib.getLib pkgs.z3}/lib/libz3${pkgs.stdenv.hostPlatform.extensions.sharedLibrary}";
            CCACHE_MAXSIZE = "100Gi";
            # The wrapper's hook sets bare names; keep full paths for clangd's query driver.
            shellHook = ''
              export CC="${clang}/bin/clang"
              export CXX="${clang}/bin/clang++"
            '';
          };
        }
      );
    };
}
