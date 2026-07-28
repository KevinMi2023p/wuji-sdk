cmake_minimum_required(VERSION 3.16)
project(wuji_sdk_cpp_demo LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# Point CMake at the extracted Wuji C SDK release:
#   cmake -S . -B build \
#     -DWUJI_SDK_INCLUDE_DIR=/path/to/sdk/include \
#     -DWUJI_SDK_LIB=/path/to/sdk/lib/libwuji_sdk_c.so
set(WUJI_SDK_INCLUDE_DIR "${CMAKE_CURRENT_SOURCE_DIR}/include" CACHE PATH
    "Directory containing wuji_sdk.h")
set(WUJI_SDK_LIB "${CMAKE_CURRENT_SOURCE_DIR}/lib/libwuji_sdk_c.so" CACHE FILEPATH
    "Path to libwuji_sdk_c.so")

if(NOT EXISTS "${WUJI_SDK_INCLUDE_DIR}/wuji_sdk.h")
    message(FATAL_ERROR
        "wuji_sdk.h not found: ${WUJI_SDK_INCLUDE_DIR}/wuji_sdk.h\n"
        "Download and extract the matching Wuji C SDK release, then set "
        "WUJI_SDK_INCLUDE_DIR.")
endif()

if(NOT EXISTS "${WUJI_SDK_LIB}")
    message(FATAL_ERROR
        "libwuji_sdk_c.so not found: ${WUJI_SDK_LIB}\n"
        "Download and extract the matching Wuji C SDK release, then set "
        "WUJI_SDK_LIB.")
endif()

find_package(Threads REQUIRED)
get_filename_component(_wuji_sdk_lib_dir "${WUJI_SDK_LIB}" DIRECTORY)
if(NOT EXISTS "${_wuji_sdk_lib_dir}/libwujihandcpp.so")
    message(FATAL_ERROR
        "libwujihandcpp.so not found beside libwuji_sdk_c.so in: "
        "${_wuji_sdk_lib_dir}\n"
        "Keep both libraries together as provided by the Wuji C SDK tarball.")
endif()

add_executable(demo_cpp demo.cpp)
target_include_directories(demo_cpp PRIVATE "${WUJI_SDK_INCLUDE_DIR}")
target_link_libraries(demo_cpp PRIVATE "${WUJI_SDK_LIB}" Threads::Threads)
target_compile_options(demo_cpp PRIVATE -Wall -Wextra -Wpedantic -O2)

# Let the build-tree executable find the extracted SDK without requiring
# LD_LIBRARY_PATH. libwuji_sdk_c.so finds libwujihandcpp.so beside itself.
set_target_properties(demo_cpp PROPERTIES
    BUILD_RPATH "${_wuji_sdk_lib_dir}"
)

message(STATUS "WUJI_SDK_INCLUDE_DIR = ${WUJI_SDK_INCLUDE_DIR}")
message(STATUS "WUJI_SDK_LIB         = ${WUJI_SDK_LIB}")
