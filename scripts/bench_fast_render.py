"""批次 A 验收基准：同一份脚本与素材，fast vs legacy 各阶段耗时对照 + 3 时间点对比帧。

沿用李世民任务 9784de85（历史 video 阶段 1493s）的 combined-1.mp4 / audio.mp3 /
subtitle.srt 作为基准输入（M3#1 同类传记基准）。

用法（仓库根目录下）：
    .venv\\Scripts\\python.exe scripts/bench_fast_render.py
"""
import json
import os
import subprocess
import sys
import time

# 相对路径依赖仓库根，无论从何处调用都切过去。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.basename(os.getcwd()) == "scripts":
    os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

from PIL import Image

from app.config import config
from app.models.schema import VideoParams
from app.services import fast_render, video
from app.utils import utils

TASK = os.path.join("storage", "tasks", "9784de85-9b2a-479a-808c-e46a90930c2b")
OUT_DIR = os.path.join("storage", "temp")
FRAME_DIR = os.path.join("docs", "screenshots", "batchA_frames")
os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(FRAME_DIR, exist_ok=True)

data = json.load(open(os.path.join(TASK, "script.json"), encoding="utf-8"))
raw = data["params"]
fields = set(VideoParams.model_fields.keys())
params = VideoParams(**{k: v for k, v in raw.items() if k in fields})

video_path = os.path.join(TASK, "combined-1.mp4")
audio_path = os.path.join(TASK, "audio.mp3")
subtitle_path = os.path.join(TASK, "subtitle.srt")

dur = fast_render.probe_duration(video_path)
print(f"BENCH input duration: {dur:.1f}s  resolution: {params.video_aspect}")
print(f"BENCH encoder(fast auto): {fast_render.probe_video_encoder()}")

fast_out = os.path.join(OUT_DIR, "bench-fast.mp4")
legacy_out = os.path.join(OUT_DIR, "bench-legacy.mp4")

# ── FAST ────────────────────────────────────────────────────────────────
config.app["render_engine"] = "fast"
fast_bd: dict = {}
t0 = time.monotonic()
ok_fast = fast_render.generate_video_fast(
    video_path, audio_path, subtitle_path, fast_out, params, None, fast_bd
)
fast_total = time.monotonic() - t0
print(f"BENCH FAST total={fast_total:.1f}s ok={ok_fast} breakdown={fast_bd}")
print(f"BENCH FAST size={os.path.getsize(fast_out)/1024/1024:.1f}MB")

# ── LEGACY ──────────────────────────────────────────────────────────────
config.app["render_engine"] = "legacy"
legacy_bd_file = os.path.join(OUT_DIR, "bench-legacy-breakdown.json")
if os.path.exists(legacy_bd_file):
    os.remove(legacy_bd_file)
os.environ["MPT_RENDER_BREAKDOWN_FILE"] = legacy_bd_file
t0 = time.monotonic()
video.generate_video(video_path, audio_path, subtitle_path, legacy_out, params, None)
legacy_total = time.monotonic() - t0
legacy_bd = {}
if os.path.exists(legacy_bd_file):
    legacy_bd = json.load(open(legacy_bd_file, encoding="utf-8"))
print(f"BENCH LEGACY total={legacy_total:.1f}s breakdown={legacy_bd}")
print(f"BENCH LEGACY size={os.path.getsize(legacy_out)/1024/1024:.1f}MB")

# ── 3 timepoint side-by-side frames ─────────────────────────────────────
ffmpeg = utils.get_ffmpeg_binary()


def grab(src, ts, out):
    subprocess.run(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{ts:.2f}",
         "-i", src, "-frames:v", "1", out],
        capture_output=True, timeout=120,
    )


for frac in (0.1, 0.5, 0.9):
    ts = dur * frac
    ff = os.path.join(OUT_DIR, f"f_{int(frac*100)}.png")
    lf = os.path.join(OUT_DIR, f"l_{int(frac*100)}.png")
    grab(fast_out, ts, ff)
    grab(legacy_out, ts, lf)
    a = Image.open(ff).convert("RGB")
    b = Image.open(lf).convert("RGB")
    h = max(a.height, b.height)
    canvas = Image.new("RGB", (a.width + b.width + 8, h), (30, 30, 30))
    canvas.paste(a, (0, 0))
    canvas.paste(b, (a.width + 8, 0))
    cmp_path = os.path.join(FRAME_DIR, f"compare_t{int(frac*100)}_fast_vs_legacy.png")
    canvas.save(cmp_path)
    print(f"BENCH frame {frac}: {cmp_path}")

summary = {
    "input_duration_s": round(dur, 1),
    "fast_total_s": round(fast_total, 1),
    "fast_breakdown": {k: round(v, 1) for k, v in fast_bd.items()},
    "fast_size_mb": round(os.path.getsize(fast_out) / 1024 / 1024, 1),
    "legacy_total_s": round(legacy_total, 1),
    "legacy_breakdown": legacy_bd,
    "legacy_size_mb": round(os.path.getsize(legacy_out) / 1024 / 1024, 1),
    "encoder": fast_render.probe_video_encoder(),
    "speedup_x": round(legacy_total / fast_total, 1) if fast_total else None,
}
with open(os.path.join(FRAME_DIR, "bench_summary.json"), "w", encoding="utf-8") as f:
    json.dump(summary, f, ensure_ascii=False, indent=2)
print("BENCH SUMMARY " + json.dumps(summary, ensure_ascii=False))
