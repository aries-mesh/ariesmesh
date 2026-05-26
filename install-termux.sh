#!/data/data/com.termux/files/usr/bin/sh
# Aries Mesh installer for Termux (Android).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/aries-mesh/ariesmesh/main/install-termux.sh | sh
#
# Aries Mesh ships with a minimal-by-default dependency set so this script
# can stay tiny on Termux: just python + git + pip install. Optional heavy
# extras (zeroconf / litellm / psutil / blake3 / aiohttp) are skipped — the
# daemon detects them at runtime and degrades to safe defaults.
set -e

REPO="aries-mesh/ariesmesh"

echo ""
echo "  Aries Mesh — Termux Installer"
echo "  ────────────────────────────────"
echo ""

if [ ! -d "/data/data/com.termux" ]; then
    echo "Error: this script is for Termux on Android only."
    echo "For macOS / Linux, use:"
    echo "  curl -fsSL https://raw.githubusercontent.com/$REPO/main/install.sh | sh"
    exit 1
fi

echo "Step 1/3: Installing system packages..."
pkg update -y
pkg install -y python git

echo ""
echo "Step 2/3: Installing Aries Mesh (minimal — pure-Python core)..."
pip install --upgrade "git+https://github.com/$REPO.git"

echo ""
echo "Step 3/3: Verifying..."
if command -v aries >/dev/null 2>&1; then
    echo ""
    echo "  Aries Mesh installed."
    echo ""
    echo "  Get started:"
    echo "    aries init --name $(hostname 2>/dev/null || echo my-phone)"
    echo "    aries start"
    echo ""
    echo "  mDNS discovery is unavailable on Termux. Add peers manually:"
    echo "    aries connect 192.168.1.42:47291"
    echo ""
else
    echo ""
    echo "  Error: the 'aries' command isn't on PATH after install."
    echo "  Try: python -m aries.cli.main --help"
    exit 1
fi
