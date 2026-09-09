include("${CMAKE_CURRENT_LIST_DIR}/common.cmake")
set(CMAKE_BUILD_TYPE Debug CACHE STRING "Unoptimized debugger build" FORCE)
set(CMAKE_C_FLAGS_DEBUG "-O0 -g" CACHE STRING "Full debug information" FORCE)
set(CMAKE_CXX_FLAGS_DEBUG "-O0 -g" CACHE STRING "Full debug information" FORCE)
