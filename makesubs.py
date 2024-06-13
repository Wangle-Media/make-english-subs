import subprocess
import whisper
import srt
from datetime import timedelta
import torch
import os
import tempfile
import argparse

# Step 1: Extract Audio with FFmpeg
def extract_audio(video_path, audio_path):
    print(f"Extracting audio from {video_path}...")
    subprocess.call(['ffmpeg', '-i', video_path, '-q:a', '0', '-map', 'a', audio_path])
    print("Audio extraction complete.")

# Step 2: Convert Audio to English Text with Timing using Whisper on GPU
def transcribe_audio_to_english(audio_path):
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Using device: {device}")
    model = whisper.load_model("large-v3", device=device)  # Using a larger model for better accuracy
    print("Starting audio transcription to text...")
    result = model.transcribe(audio_path, task="translate")
    print("Transcription complete.")
    return result['segments']

# Step 3: Save Subtitles in SRT Format
def save_subtitles(segments, srt_path):
    subtitles = []
    for i, segment in enumerate(segments):
        start = timedelta(seconds=segment['start'])
        end = timedelta(seconds=segment['end'])
        content = segment['text']
        subtitles.append(srt.Subtitle(index=i + 1, start=start, end=end, content=content))

    with open(srt_path, 'w') as f:
        f.write(srt.compose(subtitles))
    print(f"Subtitles saved to {srt_path}")

# Main function to process video and create English subtitles
def create_english_subtitles(video_path):
    # Get the base name for the SRT file
    base_name = os.path.splitext(os.path.basename(video_path))[0]
    srt_path = f"{base_name}.srt"
    
    # Use a temporary directory for the audio file
    with tempfile.TemporaryDirectory() as tmpdirname:
        audio_path = os.path.join(tmpdirname, 'extracted_audio.wav')
        extract_audio(video_path, audio_path)
        segments = transcribe_audio_to_english(audio_path)
        save_subtitles(segments, srt_path)

# Entry point for the script
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Generate English subtitles for a video file.')
    parser.add_argument('-f', '--file', required=True, help='Path to the video file')
    
    args = parser.parse_args()
    video_path = args.file
    
    create_english_subtitles(video_path)
