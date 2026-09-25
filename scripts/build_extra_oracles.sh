#!/usr/bin/env bash
set -euo pipefail

ROOT="$HOME/a3-hackathon"
CHAMPSIM="$HOME/ChampSim"

CONFIG_DIR="$ROOT/configs/M0"
BIN_DIR="$CONFIG_DIR/bin"

mkdir -p "$BIN_DIR"


build_one () {

    NAME="$1"
    CONFIG="$2"

    echo
    echo "=================================================="
    echo "BUILDING $NAME"
    echo "=================================================="

    cd "$CHAMPSIM"

    ./config.sh "$CONFIG"

    make -j"$(nproc)"

    cp -f \
        bin/champsim \
        "$BIN_DIR/$NAME"

    echo "Saved: $BIN_DIR/$NAME"
}


build_one \
    m0_latency \
    "$CONFIG_DIR/latency.json"

build_one \
    m0_mshr \
    "$CONFIG_DIR/mshr.json"

build_one \
    m0_rob \
    "$CONFIG_DIR/rob.json"


echo
echo "New binaries:"
ls -lh \
    "$BIN_DIR/m0_latency" \
    "$BIN_DIR/m0_mshr" \
    "$BIN_DIR/m0_rob"
