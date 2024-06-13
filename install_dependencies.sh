#!/bin/bash

echo "Installing dependencies..."

# Install Homebrew if not already installed
if ! command -v brew &> /dev/null
then
    echo "Homebrew not found. Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

# Install FFmpeg
echo "Installing FFmpeg..."
brew install ffmpeg

# Install Python packages
echo "Installing Python packages..."
pip install openai-whisper
pip install torch torchvision torchaudio --extra-index-url https://download.pytorch.org/whl/cu118
pip install srt

echo "All dependencies installed successfully."
