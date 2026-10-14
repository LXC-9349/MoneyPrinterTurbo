"""Per-shot narration audio with exact shot-level timing (phase 4).

Why this exists
---------------
A 3-5 minute biography is assembled from 10-20 shots, each with its own visual
and duration hint. Synthesizing the whole narration as one blob and then
splitting subtitles by estimate leaves every shot boundary approximate: a later
shot's audio can start before its visual, and subtitles drift.

Synthesizing shot by shot gives exact boundaries for free — the cumulative
duration of shots 1..n *is* shot n+1's start time. No forced alignment needed.

Behaviour
---------
* ``synthesize_shots`` returns (audio_path, total_duration, shot_timings,
  sub_maker) where ``shot_timings[i]`` is (start, end, text).
* Each shot is written to its own mp3 and concatenated; a small inter-shot
  silence keeps the narration from sounding clipped.
* Any per-shot failure falls back to `None` so the caller can use the plain
  single-pass path. This module never makes the pipeline worse.
"""
from __future__ import annotations

import os
from typing import Any

from loguru import logger

from app.utils import utils

# Pause inserted between shots so consecutive sentences do not run together.
_INTER_SHOT_SILENCE_MS = 350


def _concat_audio(segment_paths: list[str], output_path: str) -> bool:
    """Concatenate mp3 segments (with a short pause) using moviepy."""
    clips: list[Any] = []
    try:
        from moviepy import AudioFileClip, concatenate_audioclips

        for idx, path in enumerate(segment_paths):
            clips.append(AudioFileClip(path))
            if idx < len(segment_paths) - 1:
                # A tiny silent clip keeps consecutive shots from running
                # together; generated as a silent mp3 to stay format-clean.
                silence_path = f"{output_path}.gap{idx}.mp3"
                if _write_silence(silence_path):
                    clips.append(AudioFileClip(silence_path))
        final = concatenate_audioclips(clips)
        try:
            final.write_audiofile(output_path, logger=None)
        finally:
            try:
                final.close()
            except Exception:
                pass
        return os.path.isfile(output_path) and os.path.getsize(output_path) > 0
    except Exception as exc:
        logger.warning(
            f"per-shot audio concat failed: {type(exc).__name__}: {exc}"
        )
        return False
    finally:
        for clip in clips:
            try:
                clip.close()
            except Exception:
                pass


def _write_silence(path: str) -> bool:
    """Write a short silent mp3 via ffmpeg (moviepy has no silent mp3 writer)."""
    import subprocess

    from app.services import video as video_service

    ffmpeg = video_service.get_ffmpeg_binary()
    try:
        subprocess.run(
            [
                ffmpeg, "-y", "-f", "lavfi", "-i",
                "anullsrc=r=44100:cl=mono",
                "-t", f"{_INTER_SHOT_SILENCE_MS / 1000.0}",
                "-q:a", "9", "-acodec", "libmp3lame", path,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=30,
        )
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except Exception as exc:
        logger.warning(f"failed to create inter-shot silence: {type(exc).__name__}")
        return False


def synthesize_shots(
    shots: list[dict[str, Any]],
    *,
    voice_name: str,
    voice_rate: float,
    task_dir: str,
    voice_volume: float = 1.0,
) -> tuple[str, float, list[tuple[float, float, str]], Any] | tuple[None, None, None, None]:
    """Synthesize narration shot by shot.

    Returns (audio_path, total_duration, shot_timings, sub_maker) or four
    Nones when the per-shot path is unavailable for any reason.
    """
    texts = [str(s.get("narration") or "").strip() for s in shots]
    texts = [t for t in texts if t]
    if len(texts) < 2:
        # A single shot gains nothing from this path.
        return None, None, None, None

    segments_dir = os.path.join(task_dir, "audio_shots")
    os.makedirs(segments_dir, exist_ok=True)

    try:
        from moviepy import AudioFileClip
    except Exception:
        return None, None, None, None

    from app.services import tts_provider

    segment_paths: list[str] = []
    durations: list[float] = []
    for idx, text in enumerate(texts):
        seg_path = os.path.join(segments_dir, f"shot-{idx:03d}.mp3")
        try:
            # voice_volume is intentionally not forwarded: the video
            # compositing stage owns the gain, so applying it here too would
            # double it (same contract as the single-pass TTS path).
            sub_maker = tts_provider.synthesize(
                text=text,
                voice_name=voice_name,
                voice_rate=voice_rate,
                voice_file=seg_path,
            )
        except NotImplementedError:
            return None, None, None, None
        if sub_maker is None or not os.path.isfile(seg_path):
            logger.warning(f"per-shot TTS failed at shot {idx}, using single-pass path")
            return None, None, None, None
        duration = _probe_duration(seg_path, AudioFileClip)
        if duration <= 0:
            logger.warning(f"per-shot audio invalid at shot {idx}, using single-pass path")
            return None, None, None, None
        segment_paths.append(seg_path)
        durations.append(duration)

    output_path = os.path.join(task_dir, "audio.mp3")
    if not _concat_audio(segment_paths, output_path):
        return None, None, None, None

    pause = _INTER_SHOT_SILENCE_MS / 1000.0
    timings: list[tuple[float, float, str]] = []
    cursor = 0.0
    for idx, (text, duration) in enumerate(zip(texts, durations)):
        timings.append((cursor, cursor + duration, text))
        cursor += duration
        if idx < len(durations) - 1:
            cursor += pause

    total = cursor
    logger.success(
        f"per-shot audio: {len(texts)} segments, total {total:.1f}s, "
        f"{len(timings)} exact shot boundaries"
    )
    return output_path, total, timings, _NullSub()


def _probe_duration(path: str, audio_clip_cls) -> float:
    clip = None
    try:
        clip = audio_clip_cls(path)
        return float(clip.duration or 0.0)
    except Exception:
        return 0.0
    finally:
        if clip is not None:
            try:
                clip.close()
            except Exception:
                pass


class _NullSub:
    """Empty sub-maker: per-shot timings replace cue-level alignment."""

    def __init__(self) -> None:
        self.cues: list[Any] = []

    def get_srt(self) -> str:
        return ""


def timings_to_srt(timings: list[tuple[float, float, str]], wrap_width: int = 18) -> str:
    """Build an SRT from exact shot timings (one cue per shot, wrapped)."""
    lines: list[str] = []
    for idx, (start, end, text) in enumerate(timings, start=1):
        lines.append(str(idx))
        lines.append(f"{_ts(start)} --> {_ts(end)}")
        lines.append(_wrap(text, wrap_width))
        lines.append("")
    return "\n".join(lines)


def _ts(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _wrap(text: str, width: int) -> str:
    text = utils.remove_pause_tags(text or "")
    if len(text) <= width:
        return text
    # Prefer splitting on Chinese punctuation, then hard-wrap.
    for sep in ("。", "，", ",", "、", "；", ";"):
        if sep in text:
            head, _, tail = text.partition(sep)
            if head and tail:
                return f"{head}{sep}\n{_wrap(tail, width)}"
    return "\n".join(text[i:i + width] for i in range(0, len(text), width))
