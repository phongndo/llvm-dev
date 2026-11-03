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

## Agents

`AGENTS.md` (read by Codex, OpenCode and Pi; `CLAUDE.md` imports it) is the agent brief.

Two MCP servers come with the repo: [alive2-mcp](https://github.com/phongndo/alive2-mcp),
pinned in `flake.nix` and started with this worktree's `opt`, and Compiler Explorer's
`godbolt.org/mcp`. Each harness reads them from its own project file: `.mcp.json`
(Claude Code), `.codex/config.toml` (Codex), `opencode.json` (OpenCode) and
`.pi/mcp.json` (Pi, after `/trust`). In a shell, `just alive` runs the same checks.

Each agent works in its own worktree of this repo. `just setup-worktree` gives a new one
its own `llvm-project` worktree, starting from the main checkout's commit, and a build;
T3 Code runs it when it creates a worktree, and `just worktree-new <name>` does both for
other agents. The main checkout's build is then the "before": `just before opt …` runs
it, `just run opt …` runs the worktree's. `just worktrees` lists worktrees and
`just worktree-rm <path>` removes one. Builds take turns through a shared lock and share
ccache entries across worktrees; `just stale <tools>` finds outdated binaries.
