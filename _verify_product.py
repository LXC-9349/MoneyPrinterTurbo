import json
import os
import re
import subprocess
import sys

import imageio_ffmpeg

FF = imageio_ffmpeg.get_ffmpeg_exe()
TASK_DIR = r"storage/tasks/1d48c3a7-7422-4811-a1f2-9797655f1b1a"
FINAL = os.path.join(TASK_DIR, "final-1.mp4")

print("=" * 60)
print("产物真实落地 & 质量校验")
print("=" * 60)

# 1. 文件存在性与大小
size = os.path.getsize(FINAL)
print(f"[文件] final-1.mp4 存在: {os.path.exists(FINAL)}  大小: {size/1024/1024:.2f} MB")

# 2. ffmpeg -i 解析流信息（Duration / Video / Audio  codec + 分辨率）
proc = subprocess.run([FF, "-i", FINAL], capture_output=True, text=True, encoding="utf-8", errors="ignore")
info = proc.stderr
dur = re.search(r"Duration:\s*(\d+:\d+:\d+\.\d+)", info)
vstream = re.search(r"Stream #[^\n]*Video:[^\n]*", info)
astream = re.search(r"Stream #[^\n]*Audio:[^\n]*", info)
res = re.search(r"(\d{3,4})x(\d{3,4})", vstream.group(0)) if vstream else None
print(f"[时长] {dur.group(1) if dur else 'N/A'}")
print(f"[视频流] {vstream.group(0).strip() if vstream else '缺失!!'}")
print(f"[音频流] {astream.group(0).strip() if astream else '缺失!!'}")
if res:
    w, h = int(res.group(1)), int(res.group(2))
    print(f"[分辨率] {w}x{h}  竖屏9:16符合: {w==1080 and h==1920}")

# 3. 解码抽帧测试：确认画面真实可读（取中间一帧）
probe = subprocess.run(
    [FF, "-y", "-ss", "9", "-i", FINAL, "-frames:v", "1", "_frame_check.png"],
    capture_output=True, text=True, encoding="utf-8", errors="ignore",
)
frame_ok = os.path.exists("_frame_check.png") and os.path.getsize("_frame_check.png") > 1000
print(f"[抽帧] 第9秒帧成功解码: {frame_ok}")
if frame_ok:
    os.remove("_frame_check.png")

# 4. 合规清单
cfile = os.path.join(TASK_DIR, "final-1.compliance.json")
if os.path.exists(cfile):
    with open(cfile, encoding="utf-8") as f:
        compliance = json.load(f)
    print(f"[合规] AI生成标识字段: {list(compliance.keys())}")
    print("[合规] 内容:", json.dumps(compliance, ensure_ascii=False)[:400])

# 5. 字幕内容
sfile = os.path.join(TASK_DIR, "subtitle.srt")
with open(sfile, encoding="utf-8") as f:
    srt = f.read().strip()
lines = [l for l in srt.splitlines() if l and not l.isdigit() and "-->" not in l]
print(f"[字幕] 条目文本行数: {len(lines)}  示例: {lines[:2]}")

audio_ok = astream is not None
video_ok = vstream is not None and res and res.group(1) == "1080"
print("=" * 60)
print(f"结论: 视频流={video_ok} 音频流={audio_ok} 可解码={frame_ok}")
print("=" * 60)
