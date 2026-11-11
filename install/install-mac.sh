#!/bin/bash
# MakeSubs installer for macOS (Apple Silicon).
# Run in Terminal:
#   curl -fsSL https://raw.githubusercontent.com/Wangle-Media/make-english-subs/main/install/install-mac.sh | bash
# Re-running it updates MakeSubs.
set -euo pipefail

REF="${MAKESUBS_REF:-main}"
SOURCE="https://github.com/Wangle-Media/make-english-subs/archive/refs/heads/$REF.zip"
BIN="$HOME/.local/bin"

step() { printf '\n\033[36m== %s\033[0m\n' "$1"; }
# ask "question" Y|N: true for yes; Enter gives the default
ask() {
    local answer=""
    read -r -p "$1 " answer < /dev/tty || answer=""
    if [ -z "$answer" ]; then [ "$2" = "Y" ]; else [[ "$answer" =~ ^[yY] ]]; fi
}

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
# --reinstall-package: always fetch the latest code, even though the download link is the same.
uv tool install --force --reinstall-package makesubs --python 3.12 "makesubs @ $SOURCE"
uv tool update-shell >/dev/null || true

step "Optional: high-quality translation (Ollama + about 9 GB of models)"
echo "Gives much better English, enables Danish and other languages, and is needed to follow"
echo "a script."
if ask "Install it? [Y/n]" Y; then
    if [ ! -d /Applications/Ollama.app ]; then
        echo "Downloading Ollama..."
        tmp="$(mktemp -d)"
        curl -fL -o "$tmp/Ollama.zip" https://ollama.com/download/Ollama-darwin.zip
        ditto -x -k "$tmp/Ollama.zip" /Applications
        rm -rf "$tmp"
    fi
    open -a Ollama
    up=""
    for _ in $(seq 30); do
        if curl -fs http://127.0.0.1:11434/api/tags >/dev/null; then up=1; break; fi
        sleep 2
    done
    if [ -z "$up" ]; then
        echo "Ollama didn't start. Open Ollama from Applications, then run this installer again."
    fi
    # 'makesubs --setup' below downloads the models through Ollama.
fi

step "Optional: Levantine Arabic dialect model (about 6 GB)"
echo "Hears Palestinian, Lebanese, Syrian and Jordanian Arabic much better than the standard"
echo "model. On a Mac it runs on the processor, which adds a few minutes per clip."
DIALECT=()
if ask "Download it? [y/N]" N; then
    DIALECT=(--dialect levantine)
fi

step "Downloading and testing the models (several GB, once)"
"$BIN/makesubs" --setup ${DIALECT[@]+"${DIALECT[@]}"}

step "Creating MakeSubs app"
mkdir -p "$HOME/Applications"
osacompile -o "$HOME/Applications/MakeSubs.app" \
    -e "do shell script \"'$BIN/makesubs-gui' > /dev/null 2>&1 &\""

printf '\n\033[32mDone. Open MakeSubs from ~/Applications (or Spotlight).\033[0m\n'
