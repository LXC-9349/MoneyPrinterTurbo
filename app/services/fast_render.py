"""Fast FFmpeg render path (批次 A：渲染提速).

The legacy MoviePy path in :mod:`app.services.video` composites subtitles and the
compliance watermark **frame-by-frame in Python** and then does a single
``write_videofile``. For a 90s 1080x1920 film that ``final_encode`` step alone
measured ~975s (曹操) / ~1381s (李世民) on this machine.

This module replaces that hot path with pure FFmpeg work:

* Every subtitle cue is pre-rendered once to a transparent PNG using the *same*
  wrapping / font / stroke / position code as the legacy renderer
  (``video.wrap_text`` + the ``create_text_clip`` layout math), then overlaid by
  FFmpeg with ``overlay=enable='between(t,s,e)'``. The compliance watermark is
  rendered the same way and overlaid persistently.
* The voice + BGM mix is expressed as an ``amix`` filter graph (sum, matching
  MoviePy ``CompositeAudioClip``), so audio, overlays and the single video encode
  all happen in one FFmpeg invocation.
* A hardware encoder (NVENC / QSV / AMF) is auto-probed with a real 1-frame test
  encode and used when available, otherwise libx264. Output spec (resolution,
  fps, pix_fmt, BT.709 tags) and quality (crf / global_quality) stay comparable.
* Per-shot normalization for ``combine_videos`` runs through FFmpeg with a
  content-hash cache and a thread pool, then concatenates with ``-c copy`` so
  the intermediate clips are not re-encoded a second time.

The bounce ("spring") subtitle animation is intentionally **degraded** in the
fast path: overlays are static PNGs, so there is no per-frame scale animation.
This is the documented trade-off for the speed-up.

``app.services.video`` keeps the legacy MoviePy implementation intact and only
delegates here when ``app.render_engine == "fast"``; any failure in this module
falls back to the legacy path so a task is never broken by the fast path.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import lru_cache

from loguru import logger
from PIL import Image, ImageColor, ImageDraw, ImageFont

from app.config import config
from app.models.schema import (
    VideoAspect,
    VideoConcatMode,
    VideoFitMode,
    VideoTransitionMode,
)
from app.utils import file_security, utils

# Hardware encoders tried (in order) before falling back to libx264. Probe order
# favours the most common desktop accelerators first.
_HW_ENCODER_PROBE_ORDER = ("h264_nvenc", "h264_qsv", "h264_amf", "h264_videotoolbox")
_DEFAULT_CODEC = "libx264"
# Per-encoder quality offset added to the configured crf so hardware encoders
# land near the libx264 baseline bitrate (see encoder_quality_params).
_HW_QUALITY_OFFSET = {"h264_qsv": 5, "h264_nvenc": 2, "h264_amf": 3}
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def _ffmpeg_bin() -> str:
    return utils.get_ffmpeg_binary()


@contextmanager
def _timed(breakdown: dict | None, name: str):
    """Accumulate monotonic seconds into ``breakdown[name]`` (mirrors video.py)."""
    if breakdown is None:
        yield
        return
    started = time.monotonic()
    try:
        yield
    finally:
        breakdown[name] = breakdown.get(name, 0.0) + (time.monotonic() - started)


def probe_duration(path: str) -> float:
    """Return media duration in seconds by parsing ``ffmpeg -i`` stderr.

    imageio-ffmpeg ships no ffprobe, so duration is read from the banner FFmpeg
    always prints for ``-i``. Returns 0.0 when it cannot be determined.
    """
    try:
        result = subprocess.run(
            [_ffmpeg_bin(), "-hide_banner", "-i", path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0.0
    blob = (result.stderr or "") + (result.stdout or "")
    match = _DURATION_RE.search(blob)
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _configured_codec() -> str:
    from app.services import video as video_svc

    return video_svc._get_configured_video_codec()


def _configured_crf() -> int:
    from app.services import video as video_svc

    _, crf = video_svc._get_x264_encode_params()
    return crf


def encoder_quality_params(codec: str, crf: int) -> list[str]:
    """Rate-control params that keep quality/bitrate comparable across encoders.

    Hardware encoders use a different quality scale than x264 CRF: at the same
    number QSV/NVENC/AMF produce noticeably larger files than libx264 at crf N.
    A per-encoder offset is added so the fast path's output bitrate stays close
    to the libx264 baseline (the legacy MoviePy path's bitrate), satisfying the
    "keep output spec and bitrate comparable" requirement. Offsets were tuned by
    measuring output bitrate on the reference biography render.
    """
    offset = _HW_QUALITY_OFFSET.get(codec, 0)
    q = int(crf) + offset
    if codec == _DEFAULT_CODEC:
        from app.services import video as video_svc

        preset, cfg_crf = video_svc._get_x264_encode_params()
        return ["-preset", preset, "-crf", str(cfg_crf)]
    if codec == "h264_nvenc":
        return ["-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", str(q), "-b:v", "0"]
    if codec == "h264_qsv":
        return ["-preset", "medium", "-global_quality", str(q)]
    if codec == "h264_amf":
        return ["-quality", "balanced", "-rc", "cqp", "-qp_i", str(q), "-qp_p", str(q)]
    if codec == "h264_videotoolbox":
        return ["-q:v", "2"]
    return []


@lru_cache(maxsize=8)
def _encoder_probe_works(codec: str) -> bool:
    """Real 1-frame test encode with the exact params we would use for output."""
    if codec == _DEFAULT_CODEC:
        return True
    crf = _configured_crf()
    cmd = [
        _ffmpeg_bin(), "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.04:r=25",
        "-frames:v", "1", "-c:v", codec, *encoder_quality_params(codec, crf),
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


@lru_cache(maxsize=1)
def _auto_probe_encoder() -> str:
    from app.services import video as video_svc

    ffmpeg_bin = _ffmpeg_bin()
    for codec in _HW_ENCODER_PROBE_ORDER:
        if not video_svc._ffmpeg_encoder_exists(ffmpeg_bin, codec):
            continue
        if _encoder_probe_works(codec):
            logger.info(f"fast render: hardware encoder available -> {codec}")
            return codec
    return _DEFAULT_CODEC


def probe_video_encoder() -> str:
    """Return the encoder to use for the fast path.

    An explicitly configured non-default codec is honoured when it really works;
    otherwise hardware encoders are auto-probed, falling back to libx264.
    """
    configured = _configured_codec()
    if configured != _DEFAULT_CODEC:
        if _encoder_probe_works(configured):
            return configured
        logger.warning(
            f"fast render: configured codec {configured} failed probe, auto-detecting"
        )
    return _auto_probe_encoder()


def _hex_to_rgb(color) -> tuple[int, int, int]:
    if isinstance(color, str):
        try:
            return ImageColor.getcolor(color, "RGB")
        except (ValueError, TypeError):
            pass
    return (0, 0, 0)


def _resolve_subtitle_bg_color(params) -> str | None:
    value = getattr(params, "text_background_color", None)
    if isinstance(value, bool):
        return "#000000" if value else None
    return value


def _subtitle_y(params, video_height: int, block_h: int) -> int:
    """Mirror ``create_text_clip``'s vertical placement for a block of height h."""
    position = getattr(params, "subtitle_position", "bottom")
    if position == "bottom":
        return int(video_height * 0.95 - block_h)
    if position == "top":
        return int(video_height * 0.05)
    if position in ("two_thirds_bottom", "two_thirds", "2/3_bottom"):
        return int((video_height - block_h) / 3.0)
    if position == "custom":
        margin = 10
        max_y = video_height - block_h - margin
        custom = (video_height - block_h) * (getattr(params, "custom_position", 50) / 100)
        return int(max(margin, min(custom, max_y)))
    return int((video_height - block_h) / 2)


def render_subtitle_png(phrase: str, font_path: str, params, video_width: int,
                        video_height: int, out_path: str):
    """Pre-render one subtitle cue to a transparent PNG.

    Reuses ``video.wrap_text`` and the legacy layout math (font size, stroke,
    interline, rounded/rect background, position) so the fast path looks the
    same as the MoviePy path. Returns ``(path, x, y)`` or ``None`` if empty.
    """
    from app.services import video as video_svc

    phrase = (phrase or "").strip()
    if not phrase:
        return None
    font_size = int(params.font_size)
    stroke_width = int(params.stroke_width)
    max_width = int(video_width * 0.9)
    bg_color = _resolve_subtitle_bg_color(params)
    rounded = bool(getattr(params, "rounded_subtitle_background", False) and bg_color)
    has_bg = bool(bg_color)
    padding_ratio = 0.4 if rounded else 0.6
    pad_x = int(font_size * padding_ratio) if has_bg else 0
    text_max_width = max(1, max_width - 2 * pad_x)

    wrapped, _ = video_svc.wrap_text(
        phrase, max_width=text_max_width, font=font_path, fontsize=font_size
    )
    if not wrapped.strip():
        return None
    interline = int(font_size * 0.25)
    try:
        font = ImageFont.truetype(font_path, font_size)
    except Exception:
        font = ImageFont.load_default()
    text_color = getattr(params, "text_fore_color", "#FFFFFF") or "#FFFFFF"
    stroke_color = getattr(params, "stroke_color", "#000000") or "#000000"

    probe_img = Image.new("RGBA", (8, 8))
    probe_draw = ImageDraw.Draw(probe_img)
    bbox = probe_draw.multiline_textbbox(
        (0, 0), wrapped, font=font, spacing=interline,
        stroke_width=stroke_width, align="center",
    )
    text_w = max(1, int(bbox[2]) - int(bbox[0]))
    text_h = max(1, int(bbox[3]) - int(bbox[1]))

    if has_bg:
        pad_y = int(font_size * 0.35)
        box_w = max(1, int(min(max_width, text_w + 2 * pad_x)))
        box_h = max(1, int(text_h + 2 * pad_y))
        img = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        rgb = _hex_to_rgb(bg_color)
        alpha = 140 if rounded else 255
        if rounded:
            radius = max(8, int(font_size * 0.4))
            draw.rounded_rectangle(
                [0, 0, box_w - 1, box_h - 1], radius=radius, fill=(rgb[0], rgb[1], rgb[2], alpha)
            )
        else:
            draw.rectangle([0, 0, box_w - 1, box_h - 1], fill=(rgb[0], rgb[1], rgb[2], alpha))
        tx = (box_w - text_w) // 2 - int(bbox[0])
        ty = (box_h - text_h) // 2 - int(bbox[1])
        img_w, img_h = box_w, box_h
    else:
        margin = max(4, int(stroke_width) + 2)
        img_w = int(text_w + 2 * margin)
        img_h = int(text_h + 2 * margin)
        img = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        tx = margin - int(bbox[0])
        ty = margin - int(bbox[1])

    draw.multiline_text(
        (tx, ty), wrapped, font=font, fill=text_color,
        stroke_width=stroke_width, stroke_fill=stroke_color,
        spacing=interline, align="center",
    )
    img.save(out_path)
    x = (video_width - img_w) // 2
    y = _subtitle_y(params, video_height, img_h)
    return out_path, int(x), int(y)


def render_watermark_png(video_width: int, video_height: int, language: str,
                         out_path: str):
    """Render the persistent AI-disclosure mark to a transparent PNG.

    Mirrors :func:`app.services.compliance.build_watermark_clip` (white text,
    black stroke, corner position, opacity folded into the alpha channel).
    Returns ``(path, x, y)`` or ``None`` when disabled/unavailable.
    """
    try:
        from app.services import compliance
    except Exception:
        return None
    if not compliance.is_enabled():
        return None
    try:
        font_name = str(config.app.get("ai_content_label_font", "") or "").strip()
        if not font_name:
            font_name = str(config.app.get("font_name", "STHeitiMedium.ttc") or "STHeitiMedium.ttc")
        font_path = os.path.join(utils.font_dir(), font_name)
        if not os.path.isfile(font_path):
            font_path = ""
        size = int(config.app.get("ai_content_label_font_size", 28) or 28)
        opacity = float(config.app.get("ai_content_label_opacity", 0.55) or 0.55)
        opacity = max(0.15, min(1.0, opacity))
        text = compliance.label_text(language)

        font = ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default()
        probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
        bbox = probe.textbbox((0, 0), text, font=font, stroke_width=1)
        tw = max(1, int(bbox[2]) - int(bbox[0]))
        th = max(1, int(bbox[3]) - int(bbox[1]))
        margin = max(12, int(video_height * 0.02))
        pad = 4
        img = Image.new("RGBA", (tw + 2 * pad, th + 2 * pad), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.text(
            (pad - int(bbox[0]), pad - int(bbox[1])), text, font=font, fill="#FFFFFF",
            stroke_width=1, stroke_fill="#000000",
        )
        # Fold opacity into the alpha channel (matches with_opacity()).
        alpha = img.getchannel("A").point(lambda a: int(a * opacity))
        img.putalpha(alpha)
        img.save(out_path)

        position = str(config.app.get("ai_content_label_position", "bottom_right") or "bottom_right")
        img_w, img_h = img.size
        if position == "bottom_left":
            x, y = margin, video_height - img_h - margin
        elif position == "top_right":
            x, y = video_width - img_w - margin, margin
        elif position == "top_left":
            x, y = margin, margin
        else:
            x, y = video_width - img_w - margin, video_height - img_h - margin
        return out_path, int(max(0, x)), int(max(0, y))
    except Exception as exc:
        logger.warning(f"fast render: watermark PNG failed: {type(exc).__name__}: {exc}")
        return None


def _resolve_font_path(params) -> str:
    font_name = getattr(params, "font_name", "") or "STHeitiMedium.ttc"
    font_path = file_security.resolve_path_within_directory(utils.font_dir(), font_name)
    if os.name == "nt":
        font_path = font_path.replace("\\", "/")
    return font_path


def _resolve_bgm_file(params, bgm_file_override: str | None):
    """Mirror the legacy BGM resolution and return (bgm_file, loop_needed)."""
    from app.services import bgm as bgm_service
    from app.services import video as video_svc

    if not bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume):
        return "", False
    if bgm_file_override is not None:
        return bgm_file_override, False
    bgm_file = video_svc.get_bgm_file(bgm_type=params.bgm_type, bgm_file=params.bgm_file)
    return bgm_file or "", True


def _run_ffmpeg(cmd: list[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
    )


def _build_final_command(
    *,
    video_path: str,
    audio_path: str,
    bgm_file: str,
    bgm_loop: bool,
    overlays: list[tuple[str, int, int, float, float]],
    watermark: tuple[str, int, int] | None,
    voice_volume: float,
    bgm_volume: float,
    duration: float,
    codec: str,
    crf: int,
    fps: int,
    output_file: str,
) -> list[str]:
    """Assemble the single-pass FFmpeg command (overlays + audio mix + encode)."""
    inputs: list[str] = ["-i", video_path, "-i", audio_path]
    voice_idx = 1
    bgm_idx = -1
    if bgm_file:
        inputs += ["-i", bgm_file]
        bgm_idx = 2
    sub_indices: list[int] = []
    next_idx = 3 if bgm_file else 2
    for path, _x, _y, _s, _e in overlays:
        inputs += ["-i", path]
        sub_indices.append(next_idx)
        next_idx += 1
    wm_idx = -1
    if watermark:
        inputs += ["-i", watermark[0]]
        wm_idx = next_idx

    # ── video filter chain ──────────────────────────────────────────────
    vfilters: list[str] = []
    prev = "[0:v]"
    for k, (idx, (_path, x, y, start, end)) in enumerate(zip(sub_indices, overlays)):
        out = f"[vk{k}]"
        vfilters.append(
            f"{prev}[{idx}:v]overlay={x}:{y}:eof_action=repeat:"
            f"enable='between(t,{start:.3f},{end:.3f})'{out}"
        )
        prev = out
    if watermark:
        wx, wy = watermark[1], watermark[2]
        out = "[vwm]"
        vfilters.append(f"{prev}[{wm_idx}:v]overlay={wx}:{wy}:eof_action=repeat{out}")
        prev = out
    vfilters.append(f"{prev}format=yuv420p[vout]")

    # ── audio filter chain (sum, matching MoviePy CompositeAudioClip) ───
    afilters: list[str] = []
    total = max(0.1, duration)
    afilters.append(f"[{voice_idx}:a]volume={voice_volume},apad=whole_dur={total:.3f}[av]")
    if bgm_file and bgm_idx >= 0:
        fade_start = max(0.0, total - 3.0)
        if bgm_loop:
            afilters.append(
                f"[{bgm_idx}:a]aloop=loop=-1:size=2147483647,atrim=0:{total:.3f},"
                f"volume={bgm_volume},afade=t=out:st={fade_start:.3f}:d=3[ab]"
            )
        else:
            afilters.append(
                f"[{bgm_idx}:a]atrim=0:{total:.3f},volume={bgm_volume},"
                f"afade=t=out:st={fade_start:.3f}:d=3[ab]"
            )
        afilters.append("[av][ab]amix=inputs=2:duration=first:normalize=0[aout]")
    else:
        afilters.append("[av]anull[aout]")

    filter_complex = ";".join(vfilters + afilters)
    cmd = [
        _ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", codec, *encoder_quality_params(codec, crf),
        "-pix_fmt", "yuv420p",
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
        "-r", str(fps),
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
        "-movflags", "+faststart",
        "-shortest",
        output_file,
    ]
    return cmd


def generate_video_fast(
    video_path: str,
    audio_path: str,
    subtitle_path: str,
    output_file: str,
    params,
    bgm_file_override: str | None = None,
    render_breakdown: dict | None = None,
) -> bool:
    """Fast single-pass final encode. Returns whether BGM mixing succeeded.

    Same signature/return contract as :func:`app.services.video.generate_video`.
    Raises ``RuntimeError`` on encode failure so the caller can fall back to the
    legacy MoviePy path.
    """
    aspect = VideoAspect(params.video_aspect)
    video_width, video_height = aspect.to_resolution()
    fps = 30
    output_dir = os.path.dirname(os.path.abspath(output_file)) or "."
    tmp_dir = tempfile.mkdtemp(prefix="fast-render-", dir=output_dir)
    bgm_mix_succeeded = True
    try:
        overlays: list[tuple[str, int, int, float, float]] = []
        with _timed(render_breakdown, "subtitle_build"):
            if params.subtitle_enabled and subtitle_path and os.path.exists(subtitle_path):
                from app.utils.subtitle_reader import moviepy_subtitles

                font_path = _resolve_font_path(params)
                cues = moviepy_subtitles(subtitle_path)
                for i, item in enumerate(cues):
                    (start, end), text = item[0], item[1]
                    png = os.path.join(tmp_dir, f"sub{i:04d}.png")
                    rendered = render_subtitle_png(
                        text, font_path, params, video_width, video_height, png
                    )
                    if rendered:
                        overlays.append((rendered[0], rendered[1], rendered[2],
                                         float(start), float(end)))
                logger.info(f"fast render: pre-rendered {len(overlays)} subtitle PNGs")

        watermark = None
        wm_path = os.path.join(tmp_dir, "watermark.png")
        watermark = render_watermark_png(
            video_width, video_height, getattr(params, "video_language", "") or "", wm_path
        )

        bgm_file, bgm_loop = _resolve_bgm_file(params, bgm_file_override)
        if bgm_file and bgm_file_override is not None and not bgm_file:
            bgm_mix_succeeded = True

        voice_volume = 1.0 if params.voice_volume is None else float(params.voice_volume)
        bgm_volume = float(params.bgm_volume or 0.0)

        duration = probe_duration(video_path) or probe_duration(audio_path)
        if duration <= 0:
            raise RuntimeError("fast render: could not determine media duration")

        codec = probe_video_encoder()
        crf = _configured_crf()

        with _timed(render_breakdown, "final_encode"):
            tmp_out = os.path.join(tmp_dir, "final.mp4")
            cmd = _build_final_command(
                video_path=video_path, audio_path=audio_path, bgm_file=bgm_file,
                bgm_loop=bgm_loop, overlays=overlays, watermark=watermark,
                voice_volume=voice_volume, bgm_volume=bgm_volume, duration=duration,
                codec=codec, crf=crf, fps=fps, output_file=tmp_out,
            )
            logger.info(f"fast render: encoding with {codec} (duration={duration:.1f}s)")
            result = _run_ffmpeg(cmd)
            if result.returncode != 0 or not os.path.exists(tmp_out) or os.path.getsize(tmp_out) == 0:
                err = (result.stderr or result.stdout or "").strip()
                if codec != _DEFAULT_CODEC:
                    logger.warning(
                        f"fast render: {codec} failed, retrying with libx264: {err[:300]}"
                    )
                    cmd = _build_final_command(
                        video_path=video_path, audio_path=audio_path, bgm_file=bgm_file,
                        bgm_loop=bgm_loop, overlays=overlays, watermark=watermark,
                        voice_volume=voice_volume, bgm_volume=bgm_volume, duration=duration,
                        codec=_DEFAULT_CODEC, crf=crf, fps=fps, output_file=tmp_out,
                    )
                    result = _run_ffmpeg(cmd)
                if result.returncode != 0 or not os.path.exists(tmp_out) or os.path.getsize(tmp_out) == 0:
                    raise RuntimeError(
                        "fast render encode failed: " + (result.stderr or result.stdout or "")[-800:]
                    )
            os.replace(tmp_out, output_file)
        logger.success(f"fast render: wrote {output_file}")
        return bgm_mix_succeeded
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ── Fast combine: per-shot FFmpeg normalization (cached, parallel) + -c copy ──

def _render_cache_dir() -> str:
    return utils.storage_dir("cache_render", create=True)


def _fit_filter(fit_mode: VideoFitMode, w: int, h: int) -> str:
    """scale (+crop/pad) to exactly WxH, matching cover/contain semantics."""
    if fit_mode == VideoFitMode.contain:
        return (
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
        )
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},setsar=1"
    )


def _clip_identity(source: str, start: float, end: float, speed: float,
                   fit_mode: str, w: int, h: int, transition: str, codec: str) -> str:
    try:
        stat = os.stat(source)
        stamp = f"{stat.st_size}:{int(stat.st_mtime)}"
    except OSError:
        stamp = "nostat"
    key = "|".join([
        os.path.abspath(source), stamp, f"{start:.3f}", f"{end:.3f}", f"{speed:.3f}",
        fit_mode, str(w), str(h), transition, codec,
    ])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def normalize_clip_fast(
    source: str, start: float, end: float, speed: float, fit_mode: VideoFitMode,
    width: int, height: int, transition: str, codec: str, crf: int, fps: int,
    max_clip_duration: float,
) -> str | None:
    """Normalize one shot to a cached MP4 with identical encode params.

    Returns the cached file path, or ``None`` when the requested transition is
    not supported by the fast path (caller then falls back to legacy).
    """
    identity = _clip_identity(
        source, start, end, speed, getattr(fit_mode, "value", str(fit_mode)),
        width, height, transition, codec,
    )
    cache_path = os.path.join(_render_cache_dir(), f"{identity}.mp4")
    if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
        return cache_path

    seg_duration = max(0.04, end - start)
    out_duration = seg_duration / (speed if speed else 1.0)
    vf = [_fit_filter(fit_mode, width, height)]
    if speed and abs(speed - 1.0) > 1e-3:
        vf.append(f"setpts=PTS/{speed}")
    if transition in ("fade_in", "fade"):
        vf.append(f"fade=t=in:st=0:d={min(1.0, out_duration)}")
    elif transition == "fade_out":
        vf.append(f"fade=t=out:st={max(0.0, out_duration - 1.0)}:d={min(1.0, out_duration)}")
    elif transition and transition not in ("none",):
        # slide/zoom/shuffle are not reproduced by the fast path.
        return None
    vf.append(f"fps={fps}")
    filter_chain = ",".join(vf)

    tmp_fd, tmp_path = tempfile.mkstemp(prefix=".norm-", suffix=".mp4", dir=_render_cache_dir())
    os.close(tmp_fd)
    cmd = [
        _ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", source,
        "-vf", filter_chain,
        "-t", f"{min(out_duration, max_clip_duration):.3f}",
        "-an", "-c:v", codec, *encoder_quality_params(codec, crf),
        "-pix_fmt", "yuv420p",
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
        "-r", str(fps),
        tmp_path,
    ]
    try:
        result = _run_ffmpeg(cmd, timeout=600)
        if result.returncode != 0 or os.path.getsize(tmp_path) == 0:
            logger.warning(
                f"fast normalize failed for {os.path.basename(source)}: "
                f"{(result.stderr or '')[-300:]}"
            )
            return None
        os.replace(tmp_path, cache_path)
        return cache_path
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(f"fast normalize error for {os.path.basename(source)}: {exc}")
        return None
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def concat_copy_fast(clip_files: list[str], output_file: str, max_duration: float | None) -> None:
    """Concatenate already-normalized clips with ``-c copy`` (no re-encode)."""
    output_dir = os.path.dirname(os.path.abspath(output_file)) or "."
    list_fd, list_path = tempfile.mkstemp(prefix=".concat-", suffix=".txt", dir=output_dir)
    try:
        with os.fdopen(list_fd, "w", encoding="utf-8") as fp:
            for clip in clip_files:
                safe = os.path.abspath(clip).replace(os.sep, "/").replace("'", "'\\''")
                fp.write(f"file '{safe}'\n")
        cmd = [
            _ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy",
        ]
        if max_duration and max_duration > 0:
            cmd += ["-t", f"{max_duration:.3f}"]
        cmd.append(output_file)
        result = _run_ffmpeg(cmd, timeout=1800)
        if result.returncode != 0 or not os.path.exists(output_file) or os.path.getsize(output_file) == 0:
            raise RuntimeError("fast concat -c copy failed: " + (result.stderr or "")[-500:])
    finally:
        if os.path.exists(list_path):
            try:
                os.unlink(list_path)
            except OSError:
                pass


class FastPathUnsupported(Exception):
    """Raised when the fast combine cannot honour a request (caller falls back)."""


_FAST_TRANSITIONS = {"", "none", "fade_in", "fade", "fade_out"}


def combine_videos_fast(
    combined_video_path: str,
    video_paths: list[str],
    audio_file: str,
    video_aspect=VideoAspect.portrait,
    video_concat_mode=VideoConcatMode.random,
    video_transition_mode=None,
    max_clip_duration: int = 5,
    threads: int = 2,
    clip_speed: float = 1.0,
    video_fit_mode=VideoFitMode.cover,
    source_usage: dict | None = None,
    source_groups: dict | None = None,
    used_video_paths: list | None = None,
    progress_callback=None,
) -> str:
    """Fast combine: parallel cached FFmpeg normalization + ``-c copy`` concat.

    Mirrors the selection / duration-fill / looping behaviour of
    :func:`app.services.video.combine_videos` but replaces the per-clip MoviePy
    encode with a content-hash cached FFmpeg normalize and the concat re-encode
    with a stream copy. Raises :class:`FastPathUnsupported` for transitions the
    fast path does not reproduce so the caller can fall back to legacy.
    """
    import itertools

    from app.services import cancel as cancel_ctl
    from app.services import video as video_svc

    video_concat_mode = VideoConcatMode(video_concat_mode or VideoConcatMode.random)
    aspect = VideoAspect(video_aspect)
    fit_mode = VideoFitMode(video_fit_mode)
    video_width, video_height = aspect.to_resolution()

    transition_value = getattr(video_transition_mode, "value", video_transition_mode) or ""
    transition_value = str(transition_value)
    if transition_value not in _FAST_TRANSITIONS:
        raise FastPathUnsupported(f"transition '{transition_value}' not supported by fast path")

    speed = utils.normalize_clip_speed(clip_speed)
    audio_duration = probe_duration(audio_file)
    if audio_duration <= 0:
        raise FastPathUnsupported("could not determine audio duration")
    required_duration = video_svc._get_required_video_duration(audio_duration)
    source_clip_duration = max_clip_duration * speed
    codec = probe_video_encoder()
    crf = _configured_crf()
    fps = 30

    # ── segment each source into candidate sub-clips ────────────────────
    subclipped: list = []
    for path in video_paths:
        clip_duration = probe_duration(path)
        if clip_duration <= 0:
            logger.warning(f"fast combine: skipping unreadable source: {path}")
            continue
        start = 0.0
        while start < clip_duration:
            end = min(start + source_clip_duration, clip_duration)
            if end > start:
                subclipped.append(video_svc.SubClippedVideoClip(
                    file_path=path, start_time=start, end_time=end,
                    source_file_path=path,
                ))
            start = end
            if video_concat_mode.value == VideoConcatMode.sequential.value:
                break

    kwargs = ({"source_usage": source_usage, "source_groups": source_groups}
              if source_usage is not None else {})
    subclipped = video_svc._prioritize_unique_source_clips(
        subclipped_items=subclipped, concat_mode=video_concat_mode, **kwargs
    )
    if not subclipped:
        raise FastPathUnsupported("no readable clips")

    def _normalize(indexed):
        index, item = indexed
        cancel_ctl.check(stage="video")
        out = normalize_clip_fast(
            source=item.file_path, start=item.start_time, end=item.end_time,
            speed=speed, fit_mode=fit_mode, width=video_width, height=video_height,
            transition=transition_value, codec=codec, crf=crf, fps=fps,
            max_clip_duration=max_clip_duration,
        )
        if out is None:
            return None
        out_duration = min(max_clip_duration, (item.end_time - item.start_time) / (speed or 1.0))
        return video_svc.SubClippedVideoClip(
            file_path=out, duration=out_duration, width=video_width,
            height=video_height, source_file_path=item.source_file_path,
        )

    processed: list = []
    video_duration = 0.0
    workers = max(1, min(_combine_concurrency(), len(subclipped)))
    next_index = 0
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fast-clip") as executor:
        while next_index < len(subclipped) and video_duration < required_duration:
            cancel_ctl.check(stage="video")
            remaining = required_duration - video_duration
            batch = []
            batch_duration = 0.0
            candidate = next_index
            while candidate < len(subclipped) and batch_duration < remaining:
                item = subclipped[candidate]
                out_dur = min(max_clip_duration, (item.end_time - item.start_time) / (speed or 1.0))
                batch.append((candidate, item))
                batch_duration += out_dur
                candidate += 1
            if not batch:
                break
            for clip in executor.map(_normalize, batch):
                if clip is None:
                    continue
                processed.append(clip)
                video_duration += clip.duration
                video_svc._report_clip_progress(progress_callback, video_duration, required_duration)
            next_index = candidate

    if not processed:
        raise FastPathUnsupported("all clips failed to normalize")

    if video_duration < required_duration:
        base = processed.copy()
        for clip in itertools.cycle(base):
            if video_duration >= required_duration:
                break
            processed.append(clip)
            video_duration += clip.duration

    clip_files = [clip.file_path for clip in processed]
    logger.info(f"fast combine: concatenating {len(clip_files)} clips with -c copy")
    concat_copy_fast(clip_files, combined_video_path, audio_duration)

    if used_video_paths is not None:
        elapsed = 0.0
        for clip in processed:
            if elapsed >= audio_duration:
                break
            used_video_paths.append(clip.source_file_path)
            elapsed += clip.duration
    logger.info("fast combine: completed")
    return combined_video_path


def _combine_concurrency() -> int:
    """Fast path parallelizes normalization by default (bounded by CPU count)."""
    try:
        configured = int(config.app.get("video_clip_concurrency", 0))
    except (TypeError, ValueError):
        configured = 0
    if configured > 0:
        return max(1, min(8, configured))
    return max(2, min(8, (os.cpu_count() or 4)))
