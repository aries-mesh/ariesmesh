#!/bin/sh
# Aries Mesh installer for macOS and Linux.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/aries-mesh/ariesmesh/main/install.sh | sh
#
# Detects OS + architecture, downloads the matching prebuilt binary from the
# latest GitHub release, and installs it to /usr/local/bin (if writable) or
# ~/.local/bin (with a PATH hint).
set -e

REPO="aries-mesh/ariesmesh"
BINARY_NAME="aries"
INSTALL_DIR="/usr/local/bin"

# --- Detect OS and arch ----------------------------------------------------

OS="$(uname -s)"
ARCH="$(uname -m)"

case "$OS" in
    Linux)  PLATFORM="linux"  ;;
    Darwin) PLATFORM="darwin" ;;
    MINGW*|MSYS*|CYGWIN*)
        echo "Windows detected. Use PowerShell instead:"
        echo "  irm https://raw.githubusercontent.com/$REPO/main/install.ps1 | iex"
        exit 1
        ;;
    *)
        echo "Unsupported OS: $OS"
        echo "Download manually from https://github.com/$REPO/releases/latest"
        exit 1
        ;;
esac

case "$ARCH" in
    x86_64|amd64)   ARCH_SUFFIX="amd64" ;;
    arm64|aarch64)  ARCH_SUFFIX="arm64" ;;
    *)
        echo "Unsupported architecture: $ARCH"
        echo "Download manually from https://github.com/$REPO/releases/latest"
        exit 1
        ;;
esac

ASSET_NAME="aries-${PLATFORM}-${ARCH_SUFFIX}"

echo "Aries Mesh installer"
echo "  OS:           $PLATFORM"
echo "  Architecture: $ARCH_SUFFIX"
echo "  Binary:       $ASSET_NAME"

# Termux on Android exposes a writable filesystem at /data/data/com.termux —
# surface it in the installer header so the user can tell at a glance that
# the script recognized their environment.
if [ -d "/data/data/com.termux" ]; then
    echo "  Environment: Termux on Android"
fi

echo ""

# --- Find the latest release asset ----------------------------------------

if ! command -v curl >/dev/null 2>&1; then
    echo "Error: curl is required but was not found in PATH."
    exit 1
fi

RELEASE_URL="https://api.github.com/repos/$REPO/releases/latest"
DOWNLOAD_URL=$(curl -fsSL "$RELEASE_URL" \
    | grep "browser_download_url.*$ASSET_NAME\"" \
    | head -1 \
    | cut -d'"' -f4)

if [ -z "$DOWNLOAD_URL" ]; then
    # Termux fallback: no prebuilt binary for this arch yet, but Python is
    # available on Android. Install from PyPI (or git) instead. The package
    # uses optional psutil and degrades to safe defaults on Android.
    if [ -d "/data/data/com.termux" ]; then
        echo ""
        echo "No pre-built '$ASSET_NAME' found in the latest release."
        echo "Falling back to a Python install on Termux..."
        echo ""
        pkg install -y python git 2>/dev/null || true
        if pip install --upgrade aries-mesh 2>/dev/null; then
            :
        else
            echo "PyPI install failed; trying directly from git..."
            pip install --upgrade "git+https://github.com/$REPO.git"
        fi
        echo ""
        echo "Aries Mesh installed via Python."
        echo ""
        echo "Get started:"
        echo "  aries init --name $(hostname)"
        echo "  aries start"
        exit 0
    fi

    echo "Error: could not locate an asset named '$ASSET_NAME' in the latest release."
    echo "Browse available downloads:"
    echo "  https://github.com/$REPO/releases/latest"
    exit 1
fi

echo "Downloading from: $DOWNLOAD_URL"

# --- Pick an install directory --------------------------------------------

if [ -w "$INSTALL_DIR" ]; then
    TARGET="$INSTALL_DIR/$BINARY_NAME"
elif mkdir -p "$HOME/.local/bin" 2>/dev/null && [ -w "$HOME/.local/bin" ]; then
    INSTALL_DIR="$HOME/.local/bin"
    TARGET="$INSTALL_DIR/$BINARY_NAME"
    case ":$PATH:" in
        *":$HOME/.local/bin:"*) ;;
        *)
            echo ""
            echo "Note: $HOME/.local/bin is not on your PATH. Add it with:"
            echo "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc"
            echo "  (or ~/.zshrc if you use zsh)"
            echo ""
            ;;
    esac
else
    echo "Error: cannot write to /usr/local/bin or ~/.local/bin."
    echo "Try re-running with sudo, or set INSTALL_DIR to a writable directory."
    exit 1
fi

# --- Download and install -------------------------------------------------

TMP_FILE="$(mktemp)"
trap 'rm -f "$TMP_FILE"' EXIT

curl -fsSL -o "$TMP_FILE" "$DOWNLOAD_URL"
chmod +x "$TMP_FILE"
mv "$TMP_FILE" "$TARGET"

echo ""
echo "Aries Mesh installed to $TARGET"
echo ""
echo "Get started:"
echo "  aries init --name $(hostname)"
echo "  aries start"
echo ""
echo "Then open http://localhost:7272 for the dashboard."
