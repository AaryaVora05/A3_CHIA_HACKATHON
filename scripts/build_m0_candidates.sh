#!/usr/bin/env bash
set -euo pipefail

CHAMPSIM="$HOME/ChampSim"
CONFIG_DIR="$HOME/a3-hackathon/configs/M0"
BIN_DIR="$CONFIG_DIR/bin"

mkdir -p "$BIN_DIR"

build_one () {
    NAME="$1"
    CONFIG="$2"

    echo
    echo "=================================================="
    echo "BUILDING: $NAME"
    echo "CONFIG:   $CONFIG"
    echo "=================================================="

    cd "$CHAMPSIM"

    ./config.sh "$CONFIG"

    make -j"$(nproc)"

    cp -f bin/champsim "$BIN_DIR/$NAME"

    echo "Saved: $BIN_DIR/$NAME"
}


build_one m0_baseline "$CONFIG_DIR/baseline.json"
build_one m0_branch   "$CONFIG_DIR/branch.json"
build_one m0_cache    "$CONFIG_DIR/cache.json"
build_one m0_dram4    "$CONFIG_DIR/dram4.json"


echo
echo "All candidate binaries built:"
ls -lh "$BIN_DIR"
