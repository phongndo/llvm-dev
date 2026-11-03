set positional-arguments
set shell := ["bash", "-euo", "pipefail", "-c"]

# Per-machine LLVM_* values come from each host's Home Manager config in
# nix-config; the defaults here are conservative fallbacks for other machines.
source := env("LLVM_SOURCE", "llvm-project")
profile := env("LLVM_PROFILE", "dev")

# Spotlight skips *.noindex directories; build output churns too much to index.
build_root := if os() == "macos" { "build.noindex" } else { "build" }
build_dir := env("LLVM_BUILD", build_root / profile)
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

default:
    @just --list

# Show the tools and this machine's settings; no builds or configuration changes.
doctor:
    #!/usr/bin/env bash
    set -euo pipefail
    command -v clang clangd cmake ninja ccache python3 z3 creduce hyperfine alive2-mcp
    echo "build: "{{ quote(build_dir) }}" ("{{ quote(profile) }}" profile)"
    echo "jobs: "{{ quote(jobs) }}" compile, "{{ quote(link_jobs) }}" link, "{{ quote(test_jobs) }}" test"
    if [[ $(uname -s) == Linux ]]; then
      echo "memory limit: "{{ quote(if memory_max == "" { "none" } else { memory_high + " high, " + memory_max + " max" }) }}
      echo "perf_event_paranoid: $(</proc/sys/kernel/perf_event_paranoid)"
    else
      /usr/bin/xcrun --find lldb
    fi

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
      -C {{ quote("cmake/" + profile + ".cmake") }} \
      -UCMAKE_PROJECT_LLVM_INCLUDE -UHAVE_LIBPFM_INITIALIZE '-UHAVE_PERFMON_*' \
      -DCMAKE_C_COMPILER="$CC" -DCMAKE_CXX_COMPILER="$CXX" \
      "-DCMAKE_C_COMPILER_LAUNCHER=$(command -v ccache)" \
      "-DCMAKE_CXX_COMPILER_LAUNCHER=$(command -v ccache)" \
      "-DPython3_EXECUTABLE=$(command -v python3)" \
      -DLLVM_PARALLEL_COMPILE_JOBS={{ quote(jobs) }} \
      -DLLVM_PARALLEL_TABLEGEN_JOBS={{ quote(jobs) }} \
      -DLLVM_PARALLEL_LINK_JOBS={{ quote(link_jobs) }} "${platform[@]}" "$@"

build *targets="opt":
    {{ limit }} cmake --build {{ quote(build_dir) }} --parallel {{ quote(jobs) }} --target "$@"

middle-tools:
    {{ limit }} cmake --build {{ quote(build_dir) }} --parallel {{ quote(jobs) }} --target opt llvm-as llvm-dis llvm-link llvm-diff llvm-extract llvm-reduce FileCheck count not split-file llvm-config

backend-tools:
    {{ limit }} cmake --build {{ quote(build_dir) }} --parallel {{ quote(jobs) }} --target llc llvm-mc llvm-mca llvm-exegesis llvm-objdump llvm-readobj llvm-nm llvm-size llvm-symbolizer llvm-dwarfdump

# Path is relative to llvm-project/. Build needed tools first; no hidden builds.
test path *args:
    {{ limit }} python3 {{ quote(build_dir + "/bin/llvm-lit") }} -sv -j {{ quote(test_jobs) }} --timeout 120 {{ quote(source) }}/"$1" "${@:2}"

# Explicitly build prerequisites and run a full upstream suite.
check suite:
    {{ limit }} cmake --build {{ quote(build_dir) }} --parallel {{ quote(jobs) }} --target "check-$1"

run tool *args:
    @{{ quote(build_dir + "/bin") }}/"$1" "${@:2}"

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

compdb:
    @test ! -e compile_commands.json || test -L compile_commands.json
    ln -sfn "$(realpath {{ quote(build_dir + "/compile_commands.json") }})" compile_commands.json

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
    {{ limit }} cmake --build {{ quote(validation) }} --parallel {{ quote(jobs) }} --target opt llvm-config
    cmake -S "$ALIVE2_SOURCE" -B {{ quote(alive2) }} -G Ninja -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_LLVM_UTILS=ON -DBUILD_TV=OFF \
      "-DLLVM_DIR=$(realpath {{ quote(validation + "/lib/cmake/llvm") }})" \
      -DCMAKE_C_COMPILER="$CC" -DCMAKE_CXX_COMPILER="$CXX" \
      "-DCMAKE_C_COMPILER_LAUNCHER=$(command -v ccache)" \
      "-DCMAKE_CXX_COMPILER_LAUNCHER=$(command -v ccache)" \
      -DZ3_INCLUDE_DIR="$Z3_INCLUDE_DIR" -DZ3_LIBRARIES="$Z3_LIBRARY" "${platform[@]}"
    {{ limit }} cmake --build {{ quote(alive2) }} --parallel {{ quote(jobs) }} --target alive-tv alive-exec

# Uses this worktree's opt for -passes, and a local alive-tv once `just build-alive2` ran.
# Alive2 on IR: `just alive verify -passes gvn x.ll`; -opt upstream runs trunk instead.
alive *args:
    #!/usr/bin/env bash
    set -euo pipefail
    flags=(-opt {{ quote(build_dir + "/bin/opt") }})
    [[ -x {{ quote(alive2 + "/alive-tv") }} ]] && flags+=(-alive-tv {{ quote(alive2 + "/alive-tv") }})
    exec alive2-mcp "${flags[@]}" "$@"

# The Alive2 MCP server for this worktree; harnesses start it from .mcp.json and friends.
alive-mcp:
    @just alive

# List which of these tools are older than the source, so tests would use stale binaries.
stale +tools:
    #!/usr/bin/env bash
    set -euo pipefail
    for tool in "$@"; do
      plan=$(ninja -C {{ quote(build_dir) }} -n "$tool")
      if [[ $plan == *"no work to do"* ]]; then echo "fresh: $tool"; else echo "stale: $tool"; fi
    done
