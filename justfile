set positional-arguments
set shell := ["bash", "-euo", "pipefail", "-c"]

source := env("LLVM_SOURCE", "llvm-project")
build_dir := env("LLVM_BUILD", "build/dev")
profile := env("LLVM_PROFILE", "dev")
jobs := env("JOBS", "10")
link_jobs := env("LINK_JOBS", "1")
test_jobs := env("TEST_JOBS", "8")
validation := build_dir + "-validation"
alive2 := build_dir + "-alive2"

default:
    @just --list

# Show the installed tools; no builds or configuration changes.
doctor:
    @command -v clang clangd cmake ninja ccache python3 uv ty ruff z3 creduce hyperfine

# Extra arguments are ordinary CMake -D options.
configure *args:
    #!/usr/bin/env bash
    set -euo pipefail
    platform=()
    if [[ $(uname -s) == Linux ]]; then
      platform=(-DLLVM_USE_LINKER=lld "-DCMAKE_BUILD_RPATH=${LLVM_DEV_RUNTIME_LIBRARY_PATH//:/;}")
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
    cmake --build {{ quote(build_dir) }} --parallel {{ quote(jobs) }} --target "$@"

middle-tools:
    cmake --build {{ quote(build_dir) }} --parallel {{ quote(jobs) }} --target opt llvm-as llvm-dis llvm-link llvm-diff llvm-extract llvm-reduce FileCheck count not split-file llvm-config

backend-tools:
    cmake --build {{ quote(build_dir) }} --parallel {{ quote(jobs) }} --target llc llvm-mc llvm-mca llvm-exegesis llvm-objdump llvm-readobj llvm-nm llvm-size llvm-symbolizer llvm-dwarfdump

# Path is relative to llvm-project/. Build needed tools first; no hidden builds.
test path *args:
    python3 {{ quote(build_dir + "/bin/llvm-lit") }} -sv -j {{ quote(test_jobs) }} --timeout 120 {{ quote(source) }}/"$1" "${@:2}"

# Explicitly build prerequisites and run a full upstream suite.
check suite:
    cmake --build {{ quote(build_dir) }} --parallel {{ quote(jobs) }} --target "check-$1"

run tool *args:
    @{{ quote(build_dir + "/bin") }}/"$1" "${@:2}"

debug tool *args:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ $(uname -s) == Darwin ]]; then
      exec /usr/bin/xcrun lldb -- {{ quote(build_dir + "/bin") }}/"$1" "${@:2}"
    fi
    exec lldb -- {{ quote(build_dir + "/bin") }}/"$1" "${@:2}"

compdb:
    @test ! -e compile_commands.json || test -L compile_commands.json
    ln -sfn "$(realpath {{ quote(build_dir + "/compile_commands.json") }})" compile_commands.json

cache-stats:
    ccache --show-stats

# Optional, expensive first build. LLVM's normal dev build stays RTTI/EH-free.
build-alive2:
    LLVM_SOURCE={{ quote(source) }} LLVM_BUILD={{ quote(validation) }} LLVM_PROFILE=validate JOBS={{ quote(jobs) }} LINK_JOBS=1 just configure
    cmake --build {{ quote(validation) }} --parallel {{ quote(jobs) }} --target opt llvm-config
    cmake -S "$ALIVE2_SOURCE" -B {{ quote(alive2) }} -G Ninja -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_LLVM_UTILS=ON -DBUILD_TV=OFF \
      "-DLLVM_DIR=$(realpath {{ quote(validation + "/lib/cmake/llvm") }})" \
      -DCMAKE_C_COMPILER="$CC" -DCMAKE_CXX_COMPILER="$CXX" \
      "-DCMAKE_C_COMPILER_LAUNCHER=$(command -v ccache)" \
      "-DCMAKE_CXX_COMPILER_LAUNCHER=$(command -v ccache)" \
      -DZ3_INCLUDE_DIR="$Z3_INCLUDE_DIR" -DZ3_LIBRARIES="$Z3_LIBRARY"
    cmake --build {{ quote(alive2) }} --parallel {{ quote(jobs) }} --target alive-tv alive-exec

# Bounded, intraprocedural IR-pair validation; rejects incomplete/incorrect results.
prove *args:
    @python3 scripts/prove.py {{ quote(alive2 + "/alive-tv") }} {{ quote(validation + "/bin/opt") }} "$@"
