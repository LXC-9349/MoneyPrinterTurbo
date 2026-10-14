import os
import subprocess

import imageio_ffmpeg

ff = imageio_ffmpeg.get_ffmpeg_exe()
f = r"storage/tasks/1d48c3a7-7422-4811-a1f2-9797655f1b1a/final-1.mp4"
subprocess.run([ff, "-y", "-ss", "2.5", "-i", f, "-frames:v", "1", "_preview.png"], capture_output=True)
print("preview_exists", os.path.exists("_preview.png"), "size", os.path.getsize("_preview.png") if os.path.exists("_preview.png") else 0)
