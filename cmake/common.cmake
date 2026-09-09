# Build policy lives here, not in the shell or command recipes.
set(CMAKE_PROJECT_LLVM_INCLUDE "${CMAKE_CURRENT_LIST_DIR}/test-dependencies.cmake" CACHE FILEPATH "Build-only test prerequisites" FORCE)
# Command-line -D overrides come after this initial-cache file.
set(LLVM_ENABLE_PROJECTS "clang;clang-tools-extra;mlir" CACHE STRING "Enabled projects" FORCE)
set(LLVM_TARGETS_TO_BUILD "X86;AArch64" CACHE STRING "Enabled backends" FORCE)
set(LLVM_ENABLE_RUNTIMES "" CACHE STRING "Opt-in runtimes")
set(LLVM_ENABLE_ASSERTIONS ON CACHE BOOL "Check compiler invariants" FORCE)
set(LLVM_ENABLE_DUMP ON CACHE BOOL "Enable debugging dumps" FORCE)
set(LLVM_ENABLE_LTO OFF CACHE STRING "Keep development links inexpensive" FORCE)
set(BUILD_SHARED_LIBS OFF CACHE BOOL "Use LLVM's default library model" FORCE)
set(LLVM_INCLUDE_TESTS ON CACHE BOOL "Configure regression tests" FORCE)
set(LLVM_INCLUDE_EXAMPLES OFF CACHE BOOL "Examples are opt-in")
set(LLVM_INCLUDE_BENCHMARKS OFF CACHE BOOL "Benchmarks are opt-in")
set(LLVM_ENABLE_BINDINGS OFF CACHE BOOL "Language bindings are opt-in")
set(MLIR_ENABLE_BINDINGS_PYTHON OFF CACHE BOOL "MLIR Python bindings are opt-in")
set(CMAKE_EXPORT_COMPILE_COMMANDS ON CACHE BOOL "clangd compilation database" FORCE)
# Current LLVM requests PCH by default. ccache rejects it without sloppiness;
# favor correct, reusable cached objects over PCH in these development profiles.
set(CMAKE_DISABLE_PRECOMPILE_HEADERS ON CACHE BOOL "Cache-first development" FORCE)
# Compiler launchers are passed as absolute paths by configure. Do not also use
# LLVM_CCACHE_BUILD: that installs a second layer of caching via RULE_LAUNCH_COMPILE.
set(LLVM_CCACHE_BUILD OFF CACHE BOOL "Use CMake compiler launchers instead" FORCE)
