@echo off
echo Installing dependencies...

:: Install FFmpeg (assuming Windows, adjust the download link as needed)
echo Downloading FFmpeg...
curl -L -o ffmpeg.zip https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-full.7z

echo Extracting FFmpeg...
mkdir ffmpeg
7z x ffmpeg-release-full.7z -offmpeg

echo Adding FFmpeg to PATH...
setx /M PATH "%PATH%;%cd%\ffmpeg\bin"

:: Install Python packages
echo Installing Python packages...
pip install torch torchvision torchaudio --extra-index-url https://download.pytorch.org/whl/cu118
pip install git+https://github.com/openai/whisper.git
pip install srt

echo All dependencies installed successfully.
pause
