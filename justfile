set positional-arguments
set shell := ["bash", "-euo", "pipefail", "-c"]

# List commands.
default:
    @just --list

# Read-only tool, source, build, debugger, and cache diagnostics (--json available).
doctor *args:
    @python3 -B scripts/llvm_dev.py doctor "$@"

# Configure the selected build; refuse to overwrite unmanaged builds.
configure *args:
    @python3 -B scripts/llvm_dev.py configure "$@"

# Build explicit targets (default: small middle-end tool set).
build *args:
    @python3 -B scripts/llvm_dev.py build "$@"

# Build suite prerequisites and run one test path (relative to llvm-project/).
test path *args:
    @python3 -B scripts/llvm_dev.py test "$@"

# Explicit broader suite: llvm, clang, clang-tools, or mlir.
check suite *args:
    @python3 -B scripts/llvm_dev.py check "$@"

# Run a tool from the selected build, never the packaged reference compiler.
run tool *args:
    @python3 -B scripts/llvm_dev.py run "$@"

# Open an active-build tool in the platform's LLDB.
debug tool *args:
    @python3 -B scripts/llvm_dev.py debug "$@"

# Expose the active compilation database without overwriting user-managed files.
compdb *args:
    @python3 -B scripts/llvm_dev.py compdb "$@"

# Build the middle-end inspection, reduction, and regression-test utilities.
middle-tools *args:
    @python3 -B scripts/llvm_dev.py build opt llvm-as llvm-dis llvm-link llvm-diff llvm-extract llvm-reduce FileCheck count not split-file llvm-config "$@"

# Build backend disassembly, scheduling, debug-info, and instruction-benchmark tools.
backend-tools *args:
    @python3 -B scripts/llvm_dev.py build llc llvm-mc llvm-mca llvm-exegesis llvm-objdump llvm-readobj llvm-nm llvm-size llvm-symbolizer llvm-dwarfdump "$@"

# Build pinned Alive2 against a separate RTTI-enabled LLVM from this source tree.
build-alive2 *args:
    @python3 -B -m scripts.alive2 build "$@"

# Validate an explicit before/after IR pair; fail on counterexamples or incomplete proofs.
prove *args:
    @python3 -B -m scripts.alive2 prove "$@"

# Show persistent cache effectiveness; do not clear it.
cache-stats *args:
    @python3 -B scripts/llvm_dev.py cache-stats "$@"

# Fast workspace checks; no LLVM checkout or LLVM build required.
verify-workspace:
    actionlint .github/workflows/*.yml
    ruff check scripts tests
    ruff format --check scripts tests
    ty check scripts tests --python "$(command -v python3)"
    nixfmt --check flake.nix
    python3 -B -m unittest discover -s tests -v
