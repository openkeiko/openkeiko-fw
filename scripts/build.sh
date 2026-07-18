#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
MPY_DIR=${MICROPYTHON_DIR:-"$ROOT/.deps/micropython"}
BOARD_DIR="$ROOT/boards/FW1_16MB"
BUILD_DIR=${BUILD_DIR:-"$ROOT/.artifacts/micropython/build/FW1_16MB"}
TAG=v1.28.0
COMMIT=e0e9fbb17ed6fd06bb76e266ae554784c9c80804
JOBS=${JOBS:-$(sysctl -n hw.logicalcpu 2>/dev/null || echo 4)}

# Homebrew's arm-none-eabi-gcc omits newlib. Prefer the complete Nix toolchain
# when the compiler cannot resolve nosys.specs.
ARM_TOOLCHAIN_BIN=
if [[ $(arm-none-eabi-gcc -print-file-name=nosys.specs 2>/dev/null) == nosys.specs ]]; then
    if ! command -v nix >/dev/null; then
        echo "arm-none-eabi-gcc is missing newlib/nosys.specs and Nix is unavailable" >&2
        exit 1
    fi
    NIX_ARM_TOOLCHAIN=$(nix build --no-link --print-out-paths \
        nixpkgs#gcc-arm-embedded 2>/dev/null | tail -1)
    ARM_TOOLCHAIN_BIN="$NIX_ARM_TOOLCHAIN/bin"
fi

if [[ ! -d "$MPY_DIR/.git" ]]; then
    git clone --branch "$TAG" --depth 1 --recurse-submodules --shallow-submodules \
        https://github.com/micropython/micropython.git "$MPY_DIR"
fi

actual_commit=$(git -C "$MPY_DIR" rev-parse HEAD)
if [[ "$actual_commit" != "$COMMIT" ]]; then
    echo "Expected MicroPython $TAG at $COMMIT, found $actual_commit" >&2
    exit 1
fi

git -C "$MPY_DIR" submodule update --init --recursive
make -C "$MPY_DIR/mpy-cross" CC=clang STRIP=/usr/bin/strip -j"$JOBS"

if [[ -n "$ARM_TOOLCHAIN_BIN" ]]; then
    export PATH="$ARM_TOOLCHAIN_BIN:$PATH"
fi

make -C "$MPY_DIR/ports/rp2" \
    BOARD_DIR="$BOARD_DIR" \
    BUILD="$BUILD_DIR" \
    -j"$JOBS"

printf '\nFW1 MicroPython artifacts:\n'
ls -lh \
    "$BUILD_DIR"/firmware.bin \
    "$BUILD_DIR"/firmware.elf \
    "$BUILD_DIR"/firmware.elf.map \
    "$BUILD_DIR"/firmware.uf2
