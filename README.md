# llvm-dev

LLVM workspace for Apple Silicon, Linux, and WSL2. Requires Nix with flakes enabled.

```sh
git clone https://github.com/phongndo/llvm-dev.git
cd llvm-dev
git clone https://github.com/phongndo/llvm-project.git
nix develop
just doctor
just configure
just build opt
just test llvm/test/Transforms/InstSimplify
just compdb
```

`just` lists commands. The first test builds suite prerequisites and can be large.

## Builds

- `LLVM_PROFILE` picks `cmake/<profile>.cmake`: `dev` (default), `debug`,
  `dev-shared` (tools link libLLVM; smaller, cheaper links), or `validate`.
  Each profile gets its own build directory: `build/<profile>` on Linux and
  `build.noindex/<profile>` on macOS, which keeps Spotlight out of build output.
- `LLVM_JOBS`, `LLVM_LINK_JOBS`, and `LLVM_TEST_JOBS` set parallelism. On Linux,
  setting `LLVM_MEMORY_MAX` (and optionally `LLVM_MEMORY_HIGH`) runs builds and
  tests in a memory-capped systemd scope. Hosts set these in their Home Manager
  config; `just doctor` shows the values in effect.
- Built tools reference this flake's Nix store paths. After `nix flake update`,
  run `just configure` and rebuild before garbage-collecting the old shell.
- `just profile <tool> [args]` records a tool run with perf on Linux or
  Instruments on macOS.
