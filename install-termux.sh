#!/data/data/com.termux/files/usr/bin/sh
# Aries Mesh installer for Termux (Android).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/aries-mesh/ariesmesh/main/install-termux.sh | sh
#
# This script installs all build prerequisites via Termux's `pkg` (so PyNaCl
# and friends link against native libs without compiling from source), then
# installs the runtime Python dependencies, and finally fetches Aries Mesh
# from git. psutil is intentionally skipped — it isn't compatible with the
# Android runtime, and the profiler ships with a safe-defaults fallback.
set -e

REPO="aries-mesh/ariesmesh"

echo ""
echo "  Aries Mesh — Termux Installer"
echo "  ────────────────────────────────"
echo ""

# --- Guard: must be Termux -------------------------------------------------

if [ ! -d "/data/data/com.termux" ]; then
    echo "Error: this script is for Termux on Android only."
    echo "For macOS / Linux, use:"
    echo "  curl -fsSL https://raw.githubusercontent.com/$REPO/main/install.sh | bash"
    exit 1
fi

# --- Step 1/4: system packages --------------------------------------------

echo "Step 1/4: Installing system packages..."
pkg update -y
# - libsodium: native crypto backing PyNaCl
# - libffi / openssl: required by several pure-Python deps' optional C extensions
# - rust + binutils: needed to build the blake3 wheel from source if no prebuilt one exists
pkg install -y python git libsodium libffi openssl rust binutils

# --- Step 2/4: pip toolchain ----------------------------------------------

echo ""
echo "Step 2/4: Upgrading pip toolchain..."
pip install --upgrade pip setuptools wheel

# --- Step 3/4: Python deps -------------------------------------------------

echo ""
echo "Step 3/4: Installing Python dependencies..."

# PyNaCl — point it at the system libsodium we installed above.
# `--no-build-isolation` lets it see the env var; if that fails we retry plainly.
SODIUM_INSTALL=system pip install pynacl --no-build-isolation 2>/dev/null || pip install pynacl

# blake3 — needs Rust to build from source on aarch64 if no wheel.
pip install blake3 || {
    echo "  warning: blake3 install failed — SHA-256 fallback will be used."
}

# Core deps. Mostly pure-Python or have aarch64 wheels.
pip install cbor2 click httpx pydantic rich pyyaml websockets aiohttp noiseprotocol

# mDNS — sometimes flaky on Termux. Soft-fail.
pip install zeroconf || {
    echo "  warning: zeroconf install failed — mDNS discovery will be unavailable."
    echo "           use 'aries pair --code <words>' with manual peer addressing instead."
}

# litellm — large dependency tree but pure Python. Soft-fail keeps the local /
# distributed inference paths working even if cloud adapters are unavailable.
pip install litellm || {
    echo "  warning: litellm install failed — cloud LLM adapters will be unavailable."
    echo "           local inference and MockAdapter will still work."
}

# psutil is intentionally NOT installed: it isn't compatible with the Android
# runtime. aries.scheduler.profile and aries.inference.capability both detect
# its absence and return safe defaults.
echo "  note: psutil skipped (incompatible with Android). Profiler will use safe defaults."

# --- Step 4/4: Aries Mesh itself ------------------------------------------

echo ""
echo "Step 4/4: Installing Aries Mesh..."
# --no-deps because we've already installed (or skipped) every runtime dep above.
pip install "git+https://github.com/$REPO.git" --no-deps

# --- Verification ---------------------------------------------------------

if command -v aries >/dev/null 2>&1; then
    echo ""
    echo "  Aries Mesh installed successfully."
    echo ""
    echo "  Get started:"
    echo "    aries init --name $(hostname 2>/dev/null || echo my-phone)"
    echo "    aries start"
    echo ""
    echo "  Dashboard: http://localhost:7272"
    echo ""
else
    echo ""
    echo "  Warning: the 'aries' command isn't on PATH."
    echo "  You can still run the daemon via:"
    echo "    python -m aries.cli.main --help"
    echo ""
fi
