"""Regression tests for the batch-A fast FFmpeg render path (fast_render).

The fast path must (a) pre-render subtitle/watermark PNGs with the same layout
math as the legacy MoviePy renderer, (b) produce a valid single-pass encode,
(c) cache per-shot normalization by content hash, and (d) fall back to legacy
for unsupported transitions. Encoder selection is pinned to libx264 in tests so
results do not depend on host hardware.
"""
import os
import subprocess

import numpy as np
import pytest
from PIL import Image

from app.models.schema import VideoAspect, VideoFitMode, VideoTransitionMode
from app.services import fast_render, video


@pytest.fixture(autouse=True)
def _pin_libx264(monkeypatch):
    monkeypatch.setattr(fast_render, "probe_video_encoder", lambda: "libx264")
    yield


def _make_inputs(tmp_path, duration=2.0, width=480, height=854):
    binary = video.utils.get_ffmpeg_binary()
    video_file = tmp_path / "combined.mp4"
    subprocess.run(
        [binary, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", f"testsrc2=size={width}x{height}:rate=30:duration={duration}",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(video_file)],
        check=True, capture_output=True,
    )
    audio_file = tmp_path / "audio.wav"
    subprocess.run(
        [binary, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={duration}", str(audio_file)],
        check=True, capture_output=True,
    )
    subtitle_file = tmp_path / "sub.srt"
    subtitle_file.write_text(
        "1\n00:00:00,200 --> 00:00:00,900\nHello fast\n\n"
        "2\n00:00:01,200 --> 00:00:01,900\nSecond line\n",
        encoding="utf-8",
    )
    return video_file, audio_file, subtitle_file


def _params():
    from app.models.schema import VideoParams

    return VideoParams(
        video_subject="t",
        video_aspect=VideoAspect.portrait.value,
        subtitle_enabled=True,
        font_size=40,
        stroke_width=2,
    )


def test_probe_duration_parses(tmp_path):
    video_file, _, _ = _make_inputs(tmp_path, duration=2.0)
    assert fast_render.probe_duration(str(video_file)) == pytest.approx(2.0, abs=0.2)


def test_encoder_quality_params_offsets():
    # libx264 keeps the configured crf untouched.
    assert "-crf" in fast_render.encoder_quality_params("libx264", 23)
    # Hardware encoders get an offset so bitrate stays near the x264 baseline.
    qsv = fast_render.encoder_quality_params("h264_qsv", 23)
    assert str(23 + fast_render._HW_QUALITY_OFFSET["h264_qsv"]) in qsv
    nv = fast_render.encoder_quality_params("h264_nvenc", 23)
    assert str(23 + fast_render._HW_QUALITY_OFFSET["h264_nvenc"]) in nv


def test_render_subtitle_png_transparent_and_positioned(tmp_path):
    params = _params()
    out = tmp_path / "sub.png"
    result = fast_render.render_subtitle_png(
        "Hello fast", fast_render._resolve_font_path(params), params, 1080, 1920, str(out)
    )
    assert result is not None
    path, x, y = result
    assert os.path.exists(path)
    img = Image.open(path)
    assert img.mode == "RGBA"
    # Transparent background with opaque text pixels.
    alpha = np.asarray(img.getchannel("A"))
    assert alpha.min() == 0
    assert alpha.max() > 0
    # bottom position: block sits near the bottom of the frame.
    assert y > 1920 * 0.5
    # horizontally centred.
    assert abs((x + img.width / 2) - 540) < 4


def test_render_subtitle_png_empty_text_returns_none(tmp_path):
    params = _params()
    assert fast_render.render_subtitle_png(
        "   ", fast_render._resolve_font_path(params), params, 1080, 1920,
        str(tmp_path / "e.png"),
    ) is None


def test_render_watermark_png_respects_toggle(tmp_path, monkeypatch):
    from app.services import compliance

    monkeypatch.setattr(compliance, "is_enabled", lambda app_config=None: False)
    assert fast_render.render_watermark_png(480, 854, "", str(tmp_path / "w.png")) is None

    monkeypatch.setattr(compliance, "is_enabled", lambda app_config=None: True)
    result = fast_render.render_watermark_png(480, 854, "zh", str(tmp_path / "w.png"))
    assert result is not None
    path, x, y = result
    img = Image.open(path)
    assert img.mode == "RGBA"
    # default bottom_right corner.
    assert x + img.width <= 480
    assert y + img.height <= 854


def test_generate_video_fast_produces_overlaid_output(tmp_path):
    video_file, audio_file, subtitle_file = _make_inputs(tmp_path)
    out = tmp_path / "final.mp4"
    ok = fast_render.generate_video_fast(
        str(video_file), str(audio_file), str(subtitle_file), str(out), _params(), None, {}
    )
    assert ok is True
    assert out.exists() and out.stat().st_size > 0
    dur = fast_render.probe_duration(str(out))
    assert dur == pytest.approx(2.0, abs=0.4)

    # A frame inside a subtitle window must contain bright text pixels near the
    # bottom; a frame in the gap between cues must not.
    binary = video.utils.get_ffmpeg_binary()

    def frame(t):
        raw = subprocess.run(
            [binary, "-v", "error", "-ss", str(t), "-i", str(out), "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            check=True, capture_output=True,
        ).stdout
        return np.frombuffer(raw, dtype=np.uint8).reshape(854, 480, 3)

    with_sub = frame(0.5)
    without_sub = frame(1.05)
    bottom_with = with_sub[700:854]
    bottom_without = without_sub[700:854]
    bright_with = (bottom_with > 200).sum()
    bright_without = (bottom_without > 200).sum()
    assert bright_with > bright_without + 50


def test_combine_videos_fast_caches_and_concats(tmp_path, monkeypatch):
    monkeypatch.setattr(fast_render, "_render_cache_dir", lambda: str(tmp_path / "cache"))
    os.makedirs(tmp_path / "cache", exist_ok=True)
    binary = video.utils.get_ffmpeg_binary()
    srcs = []
    for i in range(2):
        src = tmp_path / f"src{i}.mp4"
        subprocess.run(
            [binary, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", f"testsrc2=size=480x854:rate=30:duration=2",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(src)],
            check=True, capture_output=True,
        )
        srcs.append(str(src))
    _, audio_file, _ = _make_inputs(tmp_path, duration=3.0)
    out = tmp_path / "combined_out.mp4"

    fast_render.combine_videos_fast(
        combined_video_path=str(out), video_paths=srcs, audio_file=str(audio_file),
        video_aspect=VideoAspect.portrait.value, max_clip_duration=2,
    )
    assert out.exists() and out.stat().st_size > 0
    first_cache = sorted(os.listdir(tmp_path / "cache"))
    assert first_cache

    # Second run must reuse the cache (no new normalized files).
    out2 = tmp_path / "combined_out2.mp4"
    fast_render.combine_videos_fast(
        combined_video_path=str(out2), video_paths=srcs, audio_file=str(audio_file),
        video_aspect=VideoAspect.portrait.value, max_clip_duration=2,
    )
    assert sorted(os.listdir(tmp_path / "cache")) == first_cache
    assert out2.exists()


def test_combine_videos_fast_rejects_unsupported_transition(tmp_path):
    with pytest.raises(fast_render.FastPathUnsupported):
        fast_render.combine_videos_fast(
            combined_video_path=str(tmp_path / "c.mp4"), video_paths=[],
            audio_file=str(tmp_path / "a.wav"),
            video_transition_mode=VideoTransitionMode.slide_in,
        )


def test_render_engine_switch_dispatch(tmp_path, monkeypatch):
    from app.config import config

    called = {"fast": 0, "legacy": 0}

    def fake_fast(*a, **k):
        called["fast"] += 1
        return True

    monkeypatch.setattr(fast_render, "generate_video_fast", fake_fast)
    monkeypatch.setattr(config.app, "__getitem__", lambda self, k: "fast" if k == "render_engine" else dict.__getitem__(self, k), raising=False)

    config.app["render_engine"] = "fast"
    assert video._render_engine_is_fast() is True
    config.app["render_engine"] = "legacy"
    assert video._render_engine_is_fast() is False
    config.app.pop("render_engine", None)
    assert video._render_engine_is_fast() is True  # default fast
