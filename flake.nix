{
  description = "Personal LLVM engineering workspace (macOS, Linux, WSL2)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  inputs.alive2-src = {
    url = "github:AliveToolkit/alive2/c5773898e4e40097b377fe9200ccb82447f4289d";
    flake = false;
  };

  outputs =
    {
      self,
      nixpkgs,
      alive2-src,
    }:
    let
      systems = [
        "aarch64-darwin"
        "x86_64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      pythonFor =
        pkgs:
        pkgs.python3.withPackages (p: [
          p.psutil
          p.pyyaml
        ]);
      # A CMake database names Nix's compiler wrapper, not its implicit SDK/C++
      # include paths. Let clangd query only these exact, pinned bootstrap drivers.
      clangdFor =
        pkgs:
        pkgs.writeShellApplication {
          name = "clangd";
          text = ''
            exec ${pkgs.llvmPackages.clang-tools}/bin/clangd \
              --query-driver=${pkgs.llvmPackages.clang}/bin/clang,${pkgs.llvmPackages.clang}/bin/clang++ "$@"
          '';
        };
      alive2SourceFor =
        pkgs:
        pkgs.runCommand "alive2-source-${alive2-src.shortRev}" { } ''
          cp -r ${alive2-src} "$out"
          chmod -R u+w "$out"
          # The immutable source has no .git. Embed the locked revision instead.
          # Like nixpkgs' Alive2 packaging, tolerate third-party warning differences.
          substituteInPlace "$out/CMakeLists.txt" \
            --replace-fail 'find_package(Git REQUIRED)' "" \
            --replace-fail 'COMMAND "''${GIT_EXECUTABLE}" describe --tags --dirty --always' \
              'COMMAND "''${CMAKE_COMMAND}" -E echo "${alive2-src.rev}"' \
            --replace-fail '-Wall -Werror -fPIC' '-Wall -fPIC'
        '';
      # nixpkgs' 4.13 release predates Zen5 PMUs; pin upstream support rather
      # than silently producing unusable llvm-exegesis on current AMD hosts.
      libpfmFor =
        pkgs:
        pkgs.libpfm.overrideAttrs {
          version = "4.13.0-unstable-91970fe";
          src = pkgs.fetchFromGitHub {
            owner = "wcohen";
            repo = "libpfm4";
            rev = "91970fe6eb4e80b63f77fb54a9592e28a207050c";
            hash = "sha256-Uhy1HhKl8WeA4uTfGSPu26FNWIgEYlQk0UeTB/7LpGk=";
          };
          # Used only on Linux; the inherited Windows patch is already upstream.
          patches = [ ];
          postPatch = "";
        };
      toolsFor =
        pkgs:
        with pkgs;
        [
          actionlint
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
          # The raw Clang package also exposes clangd; prefer our editor wrapper.
          (clangdFor pkgs)
          llvmPackages.clang-tools
          llvmPackages.clang
          ninja
          nixd
          nixfmt
          pkg-config
          (pythonFor pkgs)
          re2c
          ripgrep
          ruff
          swig
          ty
          uv
          z3
        ]
        ++ lib.optionals stdenv.hostPlatform.isLinux [
          llvmPackages.lld
          llvmPackages.lldb
          perf
        ];
    in
    {
      formatter = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        pkgs.writeShellApplication {
          name = "format-flake";
          runtimeInputs = [ pkgs.nixfmt ];
          text = ''
            if [[ "$#" -eq 0 ]]; then set -- flake.nix; fi
            exec nixfmt "$@"
          '';
        }
      );

      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          runtimeLibraries =
            with pkgs;
            [
              libffi
              libxml2
              ncurses
              zlib
              zstd
              z3
            ]
            ++ pkgs.lib.optionals pkgs.stdenv.hostPlatform.isLinux [ (libpfmFor pkgs) ];
          runtimePath = pkgs.lib.makeLibraryPath (runtimeLibraries ++ [ pkgs.stdenv.cc.cc.lib ]);
        in
        {
          default = pkgs.mkShell {
            packages = toolsFor pkgs;
            buildInputs = runtimeLibraries;

            # Configure uses this for build RPATHs. Keep the Linux loader fallback
            # for LLVM's nested native TableGen/bootstrap builds on NixOS too.
            LLVM_DEV_RUNTIME_LIBRARY_PATH = pkgs.lib.optionalString pkgs.stdenv.hostPlatform.isLinux runtimePath;
            LD_LIBRARY_PATH = pkgs.lib.optionalString pkgs.stdenv.hostPlatform.isLinux runtimePath;
            LLVM_DEV_SHELL = "1";
            LLVM_DEV_ALIVE2_SOURCE = "${alive2SourceFor pkgs}";
            LLVM_DEV_ALIVE2_REVISION = alive2-src.rev;
            LLVM_DEV_Z3_INCLUDE = "${pkgs.lib.getDev pkgs.z3}/include";
            LLVM_DEV_Z3_LIBRARY = "${pkgs.lib.getLib pkgs.z3}/lib/libz3${pkgs.stdenv.hostPlatform.extensions.sharedLibrary}";

            shellHook = ''
              export CC="${pkgs.llvmPackages.clang}/bin/clang"
              export CXX="${pkgs.llvmPackages.clang}/bin/clang++"
              export LLVM_DEV_CLANGD="${clangdFor pkgs}/bin/clangd"
              # Never put build/bin on PATH: it would replace the bootstrap Clang.
              # Entering the shell does not configure, build, or mutate Git/cache.
            '';
          };
        }
      );

      checks = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          workspace =
            pkgs.runCommand "llvm-dev-workspace-checks"
              {
                nativeBuildInputs = toolsFor pkgs;
              }
              ''
                export HOME="$TMPDIR/home"
                export CC="${pkgs.llvmPackages.clang}/bin/clang"
                export CXX="${pkgs.llvmPackages.clang}/bin/clang++"
                mkdir -p "$HOME"
                cp -r ${self} source
                chmod -R u+w source
                cd source
                just verify-workspace
                touch "$out"
              '';
        }
      );
    };
}
