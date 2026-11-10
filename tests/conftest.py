import shutil
import subprocess

import pytest


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    path = tmp_path_factory.mktemp("media") / "A001C001.mov"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                    "-t", "3", "-ac", "2", "-c:v", "mpeg4", "-c:a", "pcm_s16le",
                    "-timecode", "10:00:00:00", str(path)], check=True)
    return path
