# The dev build, but tools link libLLVM (and clang/MLIR their dylibs) instead
# of static archives: much smaller, cheaper links for a slower tool startup.
include("${CMAKE_CURRENT_LIST_DIR}/dev.cmake")
set(LLVM_LINK_LLVM_DYLIB ON CACHE BOOL "Link tools against libLLVM" FORCE)
