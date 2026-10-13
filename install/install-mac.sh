#!/bin/bash
# MakeSubs installer for macOS (Apple Silicon).
# Run in Terminal:
#   curl -fsSL https://raw.githubusercontent.com/Wangle-Media/make-english-subs/main/install/install-mac.sh | bash
# Re-running it updates MakeSubs.
set -euo pipefail

REF="${MAKESUBS_REF:-main}"
SOURCE="https://github.com/Wangle-Media/make-english-subs/archive/refs/heads/$REF.zip"
BIN="$HOME/.local/bin"
LLM_MODEL="gemma3:12b"

step() { printf '\n\033[36m== %s\033[0m\n' "$1"; }

if [ "$(uname -m)" != "arm64" ]; then
    echo "This Mac has an Intel CPU. MakeSubs will work, but slowly (no Apple Silicon acceleration)."
fi

step "Installing uv (manages Python for MakeSubs; nothing else on the system is changed)"
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$BIN:$PATH"
fi
uv --version

step "Installing MakeSubs ($REF)"
uv tool install --force --python 3.12 "makesubs @ $SOURCE"
uv tool update-shell >/dev/null || true

step "Optional: high-quality translation (Ollama + about 8 GB model)"
echo "Gives much better English, and enables Danish and other languages."
read -r -p "Install it? [Y/n] " answer < /dev/tty || answer=""
if [[ ! "$answer" =~ ^[nN] ]]; then
    if [ ! -d /Applications/Ollama.app ]; then
        echo "Downloading Ollama..."
        tmp="$(mktemp -d)"
        curl -fL -o "$tmp/Ollama.zip" https://ollama.com/download/Ollama-darwin.zip
        ditto -x -k "$tmp/Ollama.zip" /Applications
        rm -rf "$tmp"
    fi
    open -a Ollama
    for _ in $(seq 30); do
        curl -fs http://127.0.0.1:11434/api/tags >/dev/null && break
        sleep 2
    done
    /Applications/Ollama.app/Contents/Resources/ollama pull "$LLM_MODEL"
fi

step "Downloading the speech model (about 3 GB, once)"
"$BIN/makesubs" --setup

step "Creating MakeSubs app"
mkdir -p "$HOME/Applications"
osacompile -o "$HOME/Applications/MakeSubs.app" \
    -e "do shell script \"'$BIN/makesubs-gui' > /dev/null 2>&1 &\""

printf '\n\033[32mDone. Open MakeSubs from ~/Applications (or Spotlight).\033[0m\n'
