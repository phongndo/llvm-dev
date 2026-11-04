set positional-arguments
set shell := ["bash", "-euo", "pipefail", "-c"]

# Per-machine LLVM_* values come from each host's Home Manager config in
# nix-config; the defaults here are conservative fallbacks for other machines.
# cmake/<profile>.cmake: dev, debug, dev-shared (tools link libLLVM) or validate.
profile := env("LLVM_PROFILE", "dev")

# This repo holds the tools. Recipes act on the llvm-project checkout you ran just in:
# a worktree, which finds this file by searching parent folders (see install-t3), or
# llvm-project/ when run from here. Each checkout keeps its build in its own build/.
tools := source_directory()
here := shell('git -C "$1" rev-parse --show-toplevel 2>/dev/null || true', invocation_directory())
# Exported so a recipe that calls just stays on the same checkout.
export LLVM_SOURCE := env("LLVM_SOURCE", if path_exists(here / "llvm") == "true" { here } else { tools / "llvm-project" })
source := LLVM_SOURCE
root := source

# Spotlight skips *.noindex directories; build output churns too much to index.
build_root := if os() == "macos" { "build.noindex" } else { "build" }
build_dir := env("LLVM_BUILD", root / build_root / profile)
jobs := env("LLVM_JOBS", "10")
link_jobs := env("LLVM_LINK_JOBS", "1")
test_jobs := env("LLVM_TEST_JOBS", num_cpus())
validation := build_dir + "-validation"
alive2 := build_dir + "-alive2"

# On Linux hosts that set LLVM_MEMORY_MAX, builds and tests run in their own
# systemd scope: the kernel and systemd-oomd then stop the build, not services.
memory_high := env("LLVM_MEMORY_HIGH", "infinity")
memory_max := env("LLVM_MEMORY_MAX", "")
limit := if os() != "linux" { "" } else if memory_max == "" { "" } else { "systemd-run --user --scope --quiet --collect -p MemoryHigh=" + quote(memory_high) + " -p MemoryMax=" + quote(memory_max) + " -p ManagedOOMMemoryPressure=kill --" }

# Builds take turns across worktrees: one already uses every core, and two links
# at once can exhaust memory. A waiting build says so, then starts when it can.
lockfile := env("LLVM_BUILD_LOCK", cache_directory() / "llvm-dev" / "build.lock")
# Builds and tests run at low CPU priority, so the rest of the machine stays responsive.
nice := "nice -n 10"

# Paths relative to the worktree let every worktree reuse the others' ccache
# entries, since each is laid out the same way. Debug info from a shared entry
# names the worktree that compiled it first.
export CCACHE_BASEDIR := root
export CCACHE_NOHASHDIR := "1"

default:
    @just --list

# Configures a checkout the first time a recipe needs its build, so no agent has to.
_ready:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ ! -d {{ quote(source / "llvm") }} ]]; then
      echo "no LLVM checkout at "{{ quote(source) }}": run just in llvm-project or one of its worktrees" >&2
      exit 1
    fi
    if [[ ! -f {{ quote(build_dir / "build.ninja") }} ]]; then
      echo "first use of this checkout: configuring its build" >&2
      just configure >&2
    fi
    if [[ ! -e {{ quote(root / "compile_commands.json") }} && -e {{ quote(build_dir / "compile_commands.json") }} ]]; then
      just --quiet compdb
    fi

# Runs a build under the shared lock, at low priority and inside the memory limit.
_build *cmd:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p {{ quote(parent_directory(lockfile)) }}
    if ! flock -n {{ quote(lockfile) }} true; then
      echo "waiting for another worktree to finish building" >&2
    fi
    echo "$*" >&2
    exec flock {{ quote(lockfile) }} {{ nice }} {{ limit }} "$@"

# Show the tools and this machine's settings; no builds or configuration changes.
doctor:
    #!/usr/bin/env bash
    set -euo pipefail
    command -v clang clangd cmake ninja ccache flock python3 z3 creduce hyperfine alive2-mcp
    echo "worktree: "{{ quote(root) }}
    echo "build: "{{ quote(build_dir) }}" ("{{ quote(profile) }}" profile)"
    echo "jobs: "{{ quote(jobs) }}" compile, "{{ quote(link_jobs) }}" link, "{{ quote(test_jobs) }}" test"
    if [[ $(uname -s) == Linux ]]; then
      echo "memory limit: "{{ quote(if memory_max == "" { "none" } else { memory_high + " high, " + memory_max + " max" }) }}
      echo "perf_event_paranoid: $(</proc/sys/kernel/perf_event_paranoid)"
    else
      /usr/bin/xcrun --find lldb
    fi

# Built tools reference this flake's Nix store paths: after `nix flake update`,
# reconfigure and rebuild before garbage-collecting the old shell.
# Extra arguments are ordinary CMake -D options.
configure *args:
    #!/usr/bin/env bash
    set -euo pipefail
    platform=()
    if [[ $(uname -s) == Linux ]]; then
      # Split DWARF keeps debug info out of links: less link time, memory and disk.
      platform=(-DLLVM_USE_LINKER=lld -DLLVM_USE_SPLIT_DWARF=ON
        "-DCMAKE_BUILD_RPATH=${LLVM_DEV_RUNTIME_LIBRARY_PATH//:/;}")
    fi
    cmake -S {{ quote(source + "/llvm") }} -B {{ quote(build_dir) }} -G Ninja \
      -C {{ quote(tools / "cmake" / (profile + ".cmake")) }} \
      -UCMAKE_PROJECT_LLVM_INCLUDE -UHAVE_LIBPFM_INITIALIZE '-UHAVE_PERFMON_*' \
      -DCMAKE_C_COMPILER="$CC" -DCMAKE_CXX_COMPILER="$CXX" \
      "-DCMAKE_C_COMPILER_LAUNCHER=$(command -v ccache)" \
      "-DCMAKE_CXX_COMPILER_LAUNCHER=$(command -v ccache)" \
      "-DPython3_EXECUTABLE=$(command -v python3)" \
      -DLLVM_PARALLEL_COMPILE_JOBS={{ quote(jobs) }} \
      -DLLVM_PARALLEL_TABLEGEN_JOBS={{ quote(jobs) }} \
      -DLLVM_PARALLEL_LINK_JOBS={{ quote(link_jobs) }} "${platform[@]}" "$@"

build *targets="opt": _ready
    @just _build cmake --build {{ quote(build_dir) }} --parallel {{ quote(jobs) }} --target "$@"

middle-tools: _ready
    @just _build cmake --build {{ quote(build_dir) }} --parallel {{ quote(jobs) }} --target opt llvm-as llvm-dis llvm-link llvm-diff llvm-extract llvm-reduce FileCheck count not split-file llvm-config

backend-tools: _ready
    @just _build cmake --build {{ quote(build_dir) }} --parallel {{ quote(jobs) }} --target llc llvm-mc llvm-mca llvm-exegesis llvm-objdump llvm-readobj llvm-nm llvm-size llvm-symbolizer llvm-dwarfdump

# Path is relative to llvm-project/. Build needed tools first; no hidden builds.
test path *args: _ready
    {{ nice }} {{ limit }} python3 {{ quote(build_dir + "/bin/llvm-lit") }} -sv -j {{ quote(test_jobs) }} --timeout 120 {{ quote(source) }}/"$1" "${@:2}"

# Explicitly build prerequisites and run a full upstream suite.
check suite: _ready
    @just _build cmake --build {{ quote(build_dir) }} --parallel {{ quote(jobs) }} --target "check-$1"

run tool *args: _ready
    @{{ quote(build_dir + "/bin") }}/"$1" "${@:2}"

# Run a tool from the main checkout's build: the "before" to this worktree's run.
before tool *args:
    #!/usr/bin/env bash
    set -euo pipefail
    main=$(dirname "$(git -C {{ quote(source) }} rev-parse --path-format=absolute --git-common-dir)")
    bin="$main"/{{ quote(build_root / profile / "bin") }}/"$1"
    if [[ ! -x "$bin" ]]; then
      echo "$bin is not built: run just sync" >&2
      exit 1
    fi
    exec "$bin" "${@:2}"

# New T3 threads start from the main checkout's main, and its build is their "before".
# Move the main checkout to upstream's latest main and build it.
sync:
    #!/usr/bin/env bash
    set -euo pipefail
    main=$(dirname "$(git -C {{ quote(source) }} rev-parse --path-format=absolute --git-common-dir)")
    git -C "$main" fetch --quiet --no-tags upstream main
    git -C "$main" switch --quiet main
    git -C "$main" merge --quiet --ff-only upstream/main
    LLVM_SOURCE="$main" just middle-tools
    echo "main checkout built at $(git -C "$main" rev-parse --short HEAD)"

debug tool *args:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ $(uname -s) == Darwin ]]; then
      exec /usr/bin/xcrun lldb -- {{ quote(build_dir + "/bin") }}/"$1" "${@:2}"
    fi
    exec lldb -- {{ quote(build_dir + "/bin") }}/"$1" "${@:2}"

# Sample a tool run: perf on Linux, Instruments' Time Profiler on macOS.
profile tool *args:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p {{ quote(build_dir + "/profiles") }}
    out={{ quote(build_dir + "/profiles") }}/"$1-$(date +%Y%m%d-%H%M%S)"
    if [[ $(uname -s) == Darwin ]]; then
      /usr/bin/xcrun xctrace record --template 'Time Profiler' --output "$out.trace" \
        --launch -- {{ quote(build_dir + "/bin") }}/"$1" "${@:2}"
      echo "open $out.trace"
    else
      # LLVM omits frame pointers; DWARF unwinding gives complete call stacks.
      perf record --call-graph=dwarf -o "$out.data" -- {{ quote(build_dir + "/bin") }}/"$1" "${@:2}"
      echo "perf report -i $out.data"
    fi

# Links the build's compile_commands.json at the worktree's root, where clangd finds it.
compdb:
    @test ! -e {{ quote(root / "compile_commands.json") }} || test -L {{ quote(root / "compile_commands.json") }}
    ln -sfn "$(realpath {{ quote(build_dir + "/compile_commands.json") }})" {{ quote(root / "compile_commands.json") }}

cache-stats:
    ccache --show-stats

# Optional, expensive first build. LLVM's normal dev build stays RTTI/EH-free.
build-alive2:
    #!/usr/bin/env bash
    set -euo pipefail
    platform=()
    if [[ $(uname -s) == Linux ]]; then
      platform=("-DCMAKE_BUILD_RPATH=${LLVM_DEV_RUNTIME_LIBRARY_PATH//:/;}")
    fi
    LLVM_SOURCE={{ quote(source) }} LLVM_BUILD={{ quote(validation) }} LLVM_PROFILE=validate \
      LLVM_JOBS={{ quote(jobs) }} LLVM_LINK_JOBS=1 just configure
    just _build cmake --build {{ quote(validation) }} --parallel {{ quote(jobs) }} --target opt llvm-config
    cmake -S "$ALIVE2_SOURCE" -B {{ quote(alive2) }} -G Ninja -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_LLVM_UTILS=ON -DBUILD_TV=OFF \
      "-DLLVM_DIR=$(realpath {{ quote(validation + "/lib/cmake/llvm") }})" \
      -DCMAKE_C_COMPILER="$CC" -DCMAKE_CXX_COMPILER="$CXX" \
      "-DCMAKE_C_COMPILER_LAUNCHER=$(command -v ccache)" \
      "-DCMAKE_CXX_COMPILER_LAUNCHER=$(command -v ccache)" \
      -DZ3_INCLUDE_DIR="$Z3_INCLUDE_DIR" -DZ3_LIBRARIES="$Z3_LIBRARY" "${platform[@]}"
    just _build cmake --build {{ quote(alive2) }} --parallel {{ quote(jobs) }} --target alive-tv alive-exec

# Uses this worktree's opt for -passes, and a local alive-tv once `just build-alive2` ran.
# Alive2 on IR: `just alive verify -passes gvn x.ll`; -opt upstream runs trunk instead.
alive *args:
    #!/usr/bin/env bash
    set -euo pipefail
    flags=(-opt {{ quote(build_dir + "/bin/opt") }})
    [[ -x {{ quote(alive2 + "/alive-tv") }} ]] && flags+=(-alive-tv {{ quote(alive2 + "/alive-tv") }})
    cd {{ quote(invocation_directory()) }} # IR paths are relative to where you ran just
    exec alive2-mcp "${flags[@]}" "$@"

# The Alive2 MCP server for this worktree; harnesses start it from .mcp.json and friends.
alive-mcp:
    @cd {{ quote(invocation_directory()) }} && just --justfile {{ quote(justfile()) }} alive

# llvm-reduce keeps each cut only if Alive2 still calls the result incorrect, so
# it never shrinks into IR that merely fails to parse. Uses local alive-tv once
# built, else alive2.llvm.org.
# Shrink IR that passes miscompile: `just reduce gvn big.ll` writes big.reduced.ll;
# extra arguments go to `alive verify`, e.g. `-opt upstream` to reduce against trunk.
reduce passes file *alive: _ready
    #!/usr/bin/env bash
    set -euo pipefail
    just build llvm-reduce opt >&2
    cd {{ quote(invocation_directory()) }}
    test=$(mktemp "${TMPDIR:-/tmp}/reduce-test.XXXXXX")
    trap 'rm -f "$test"' EXIT
    extra=""
    if (($# > 2)); then extra=$(printf ' %q' "${@:3}"); fi
    # No pipefail here: alive exits 1 for incorrect, and grep's status decides.
    printf '#!/usr/bin/env bash\njust --justfile %q alive verify%s -passes %q "$1" 2>/dev/null | head -1 | grep -q "^incorrect "\n' \
      {{ quote(justfile()) }} "$extra" "$1" > "$test"
    chmod +x "$test"
    if ! "$test" "$2"; then
      echo "$2 is not miscompiled by -passes=$1, so there is nothing to reduce" >&2
      exit 1
    fi
    out="${2%.ll}.reduced.ll"
    {{ quote(build_dir / "bin/llvm-reduce") }} --test "$test" -j 2 -o "$out" "$2" >&2
    echo "$out: $(grep -c . "$2") -> $(grep -c . "$out") lines, still miscompiled by -passes=$1"

# List which of these tools are older than the source, so tests would use stale binaries.
stale +tools: _ready
    #!/usr/bin/env bash
    set -euo pipefail
    for tool in "$@"; do
      plan=$(ninja -C {{ quote(build_dir) }} -n "$tool")
      if [[ $plan == *"no work to do"* ]]; then echo "fresh: $tool"; else echo "stale: $tool"; fi
    done

# Once per machine: T3 keeps llvm-project's worktrees in this folder, so these files are
# what just, direnv and Claude find there by searching parent folders. Nothing is written
# into a worktree, and edits to this repo reach every worktree at once. The Claude hook
# loads the folder's direnv shell into Claude's Bash tool, for any project with an .envrc.
# Wire T3's llvm-project worktrees up to this repo.
install-t3:
    #!/usr/bin/env bash
    set -euo pipefail
    dir="${T3CODE_HOME:-$HOME/.t3}/worktrees/llvm-project"
    mkdir -p "$dir"
    printf "import '%s/justfile'\n" {{ quote(tools) }} > "$dir/justfile"
    printf 'use flake %s\n' {{ quote(tools) }} > "$dir/.envrc"
    ln -sfn {{ quote(tools / "AGENTS.md") }} "$dir/CLAUDE.md"
    ln -sfn {{ quote(tools / ".mcp.json") }} "$dir/.mcp.json"
    direnv allow {{ quote(tools) }}
    direnv allow "$dir"
    settings="$HOME/.claude/settings.json"
    [[ -f "$settings" ]] || echo '{}' > "$settings"
    hook='test -z "$CLAUDE_ENV_FILE" || direnv export bash >> "$CLAUDE_ENV_FILE" 2>/dev/null; true'
    updated=$(jq --arg hook "$hook" '
      if any(.hooks.SessionStart[]?.hooks[]?; .command == $hook) then .
      else .hooks.SessionStart += [{hooks: [{type: "command", command: $hook, timeout: 300}]}] end
    ' "$settings")
    printf '%s\n' "$updated" > "$settings"
    echo "wired up $dir"

# It finds this repo the same way T3's worktrees do. Pass a ref such as upstream/main
# to start elsewhere.
# For agents outside T3: a worktree at trees/<name> on branch <name>.
worktree-new name base="main":
    git -C {{ quote(source) }} worktree add --quiet -b "$1" {{ quote(tools / "trees") }}/"$1" "$2"

# Each llvm-project worktree's branch, uncommitted files and build size.
worktrees:
    #!/usr/bin/env bash
    set -euo pipefail
    git -C {{ quote(source) }} worktree prune
    git -C {{ quote(source) }} worktree list --porcelain | sed -n 's/^worktree //p' | while read -r dir; do
      printf '%-60s %-32s changed=%-3s build=%s\n' "$dir" "$(git -C "$dir" branch --show-current)" \
        "$(git -C "$dir" status --porcelain | wc -l | tr -d ' ')" \
        "$(du -sh "$dir"/{{ build_root }} 2>/dev/null | cut -f1)"
    done

# Remove a worktree and its build. Git refuses while it has uncommitted changes; its branch stays.
worktree-rm path:
    cd {{ quote(invocation_directory()) }} && git -C {{ quote(source) }} worktree remove "$(realpath "$1")"
