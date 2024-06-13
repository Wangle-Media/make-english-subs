import subprocess
import whisper
import srt
from datetime import timedelta

# Step 1: Extract Audio with FFmpeg
def extract_audio(video_path, audio_path):
    subprocess.call(['ffmpeg', '-i', video_path, '-q:a', '0', '-map', 'a', audio_path])

# Step 2: Convert Audio to English Text with Timing using Whisper
def transcribe_audio_to_english(audio_path):
    model = whisper.load_model("large")  # Using a larger model for better accuracy
    result = model.transcribe(audio_path, task="translate")
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

# Main function to process video and create English subtitles
def create_english_subtitles(video_path, audio_path, srt_path):
    extract_audio(video_path, audio_path)
    segments = transcribe_audio_to_english(audio_path)
    save_subtitles(segments, srt_path)

# Example usage
video_path = 'WIKITONGUESPascalspeakingGerman.mp4'
audio_path = 'extracted_audio.wav'
srt_path = 'subtitles.srt'

create_english_subtitles(video_path, audio_path, srt_path)
