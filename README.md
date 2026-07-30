# wuji-sdk

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE) [![Release](https://img.shields.io/github/v/release/wuji-technology/wuji-sdk?cacheSeconds=3600)](https://github.com/wuji-technology/wuji-sdk/releases) ![Coverage](https://raw.githubusercontent.com/wuji-technology/wuji-sdk/badges/coverage.svg)

SDKs for Wuji series devices (Wuji Glove, WujiHand, Wuji Hand 2, and other peripherals): automatic device discovery, connection management, and real-time data streaming — rich hand-tracking data (joint angles, skeleton, fingertip poses), tactile/EMF sensing, multi-channel MCAP recording, and hand retargeting (map keypoints to hand joint commands).

## SDKs

| Language | Install | Docs | Examples |
|----------|---------|------|----------|
| **Python** | `pip install wuji-sdk` | [examples/python/README.md](examples/python/README.md) | [examples/python/](examples/python/) |
| **C** | Prebuilt tarball on each [Release](https://github.com/wuji-technology/wuji-sdk/releases) | [examples/c/README.md](examples/c/README.md) | [examples/c/](examples/c/) |

The Python SDK is the primary, full-featured interface. The C SDK exposes a C API (`libwuji_sdk_c.so` + `wuji_sdk.h`) for native/embedded integration.

## Hand Motion Demo

[`demo.cpp`](demo.cpp) is a C++17 port of [`demo.py`](demo.py). It uses the
C++-compatible header from the released C SDK. Each demo discovers both sides,
skips a missing left or right hand, and performs the same 150 Hz grasp cycle,
middle-finger gesture, return to neutral, and safe motor cleanup on every hand
it finds.

> **Warning:** Both demos move every physical Wuji Hand 2 they find. Keep all
> hands clear. Press Ctrl+C to stop; the demo closes its publishers and disables
> the motors.

Run the Python demo with:

```bash
python demo.py
```

When hands are connected directly through separate Linux Ethernet interfaces
that use the same subnet, the Python demo detects which interface reaches each
hand and asks for `sudo` access to install temporary `/32` host routes. It
removes only those routes after disabling and disconnecting the hands. Pass
`--no-auto-routes` if routing is already managed externally. For a permanent
setup, configure the host routes in NetworkManager or connect both hands and one
computer interface through the same Ethernet switch.

You need CMake 3.16 or newer, a C++17 compiler, and the SDK runtime libraries.
On Ubuntu/Debian, install the build and runtime prerequisites with:

```bash
sudo apt install build-essential cmake curl libusb-1.0-0 libudev1
```

This example is verified with the v2026.7.21 C SDK release used by this
repository. For x86-64 GNU/Linux:

```bash
curl -fL \
  "https://github.com/wuji-technology/wuji-sdk/releases/download/v2026.7.21/wuji-sdk-c-2026.7.21-x86_64-linux-gnu.tar.gz" \
  | tar xz

SDK_DIR="$PWD/wuji-sdk-c-2026.7.21-x86_64-linux-gnu"
```

On ARM64 GNU/Linux, use the `aarch64-linux-gnu` tarball and directory name instead.
The Linux SDK requires glibc 2.35 or newer. Keep `libwuji_sdk_c.so` and
`libwujihandcpp.so` together in the extracted `lib/` directory.

Configure and compile:

```bash
cmake -S . -B build \
  -DWUJI_SDK_INCLUDE_DIR="$SDK_DIR/include" \
  -DWUJI_SDK_LIB="$SDK_DIR/lib/libwuji_sdk_c.so"
cmake --build build -j
```

Run:

```bash
./build/demo_cpp
```

The build embeds the SDK library directory in the executable's build RPATH, so
`LD_LIBRARY_PATH` is not needed. For a direct compiler invocation instead:

```bash
g++ -std=c++17 -O2 -Wall -Wextra -Wpedantic \
  -I"$SDK_DIR/include" demo.cpp \
  -L"$SDK_DIR/lib" -Wl,-rpath,"$SDK_DIR/lib" \
  -lwuji_sdk_c -pthread -o demo_cpp
./demo_cpp
```

## Repository Structure

```text
├── demo.py                    # Wuji Hand 2 motion demo (Python)
├── demo.cpp                   # Equivalent motion demo (C++17)
├── CMakeLists.txt             # Builds demo_cpp against the released C SDK
├── examples/
│   ├── python/              # Python SDK docs (README) + examples (pip install wuji-sdk)
│   │   ├── README.md
│   │   ├── wuji_glove/
│   │   ├── wuji_hand/
│   │   ├── wuji_hand_2/
│   │   └── retargeting/     # map hand keypoints → joint commands
│   └── c/                   # C SDK docs (README) + examples (prebuilt tarball from Releases)
│       ├── README.md
│       ├── wuji_glove/
│       ├── wuji_hand/
│       ├── wuji_hand_2/
│       └── retargeting/
├── CHANGELOG.md             # Version history (Python + C SDK)
├── LICENSE
└── README.md
```

## Documentation

For detailed documentation, see the [Wuji Docs Center](https://docs.wuji.tech/docs/en/wuji-glove/latest/).

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for the version history of both SDKs.

## Contact

For any questions, please contact [support@wuji.tech](mailto:support@wuji.tech).

## License

[MIT](LICENSE)
