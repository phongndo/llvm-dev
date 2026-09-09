{
  description = "LLVM development tools";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  inputs.alive2-src = {
    url = "github:AliveToolkit/alive2/c5773898e4e40097b377fe9200ccb82447f4289d";
    flake = false;
  };

  outputs =
    { nixpkgs, alive2-src, ... }:
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
          clang = pkgs.llvmPackages.clang;
          # Query only the pinned compiler wrappers for Nix's implicit SDK/headers.
          clangd = pkgs.writeShellApplication {
            name = "clangd";
            text = ''
              exec ${pkgs.llvmPackages.clang-tools}/bin/clangd \
                --query-driver=${clang}/bin/clang,${clang}/bin/clang++ "$@"
            '';
          };
          alive2 = pkgs.runCommand "alive2-source-${alive2-src.shortRev}" { } ''
            cp -r ${alive2-src} "$out"
            chmod -R u+w "$out"
            # Immutable source has no Git metadata; keep third-party warnings nonfatal.
            substituteInPlace "$out/CMakeLists.txt" \
              --replace-fail 'find_package(Git REQUIRED)' "" \
              --replace-fail 'COMMAND "''${GIT_EXECUTABLE}" describe --tags --dirty --always' \
                'COMMAND "''${CMAKE_COMMAND}" -E echo "${alive2-src.rev}"' \
              --replace-fail '-Wall -Werror -fPIC' '-Wall -fPIC'
          '';
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
          runtimePath = lib.makeLibraryPath (libraries ++ [ pkgs.stdenv.cc.cc.lib ]);
        in
        {
          default = pkgs.mkShell {
            packages =
              with pkgs;
              [
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
                clang
                ninja
                nixd
                nixfmt
                pkg-config
                (python3.withPackages (p: [
                  p.psutil
                  p.pyyaml
                ]))
                re2c
                ripgrep
                ruff
                swig
                ty
                uv
                z3
              ]
              ++ lib.optionals linux [
                llvmPackages.lld
                llvmPackages.lldb
                perf
              ];
            buildInputs = libraries;
            LLVM_DEV_RUNTIME_LIBRARY_PATH = lib.optionalString linux runtimePath;
            LD_LIBRARY_PATH = lib.optionalString linux runtimePath;
            LLVM_DEV_CLANGD = "${clangd}/bin/clangd";
            ALIVE2_SOURCE = "${alive2}";
            Z3_INCLUDE_DIR = "${lib.getDev pkgs.z3}/include";
            Z3_LIBRARY = "${lib.getLib pkgs.z3}/lib/libz3${pkgs.stdenv.hostPlatform.extensions.sharedLibrary}";
            CCACHE_MAXSIZE = "100Gi";
            CCACHE_COMPRESS = "true";
            shellHook = ''
              export CC="${clang}/bin/clang"
              export CXX="${clang}/bin/clang++"
            '';
          };
        }
      );
    };
}
