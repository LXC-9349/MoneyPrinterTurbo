"""Compliance for AI-generated biography videos (phase 5).

Two obligations this module covers:

1. **AI-content disclosure.** Chinese platforms (抖音/B站) and YouTube require
   synthetic-content labelling. We burn a persistent corner mark into the video
   plus an end-card, because a platform-agnostic upload cannot rely on a
   platform-side toggle that may change.

2. **Provenance sidecar.** A JSON file next to the video listing every source
   image with its license, plus the LLM/TTS models used, so a human can review
   the video for copyright compliance without re-running the pipeline.

Both are additive: any failure logs a warning and the video still ships.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from loguru import logger

AI_LABEL_TEXT = "内容由AI生成"
AI_LABEL_TEXT_EN = "AI-generated content"

# Corner mark defaults: bottom-right, small and semi-transparent so it does not
# fight the subtitles that sit near the bottom centre.
DEFAULT_MARK_POSITION = "bottom_right"
DEFAULT_MARK_OPACITY = 0.55
DEFAULT_MARK_FONT_SIZE = 28


def is_enabled(app_config: dict | None = None) -> bool:
    from app.config import config as _config

    cfg = app_config if app_config is not None else _config.app
    return bool(cfg.get("ai_content_label_enabled", True))


def label_text(language: str = "") -> str:
    from app.config import config as _config

    custom = str(_config.app.get("ai_content_label_text", "") or "").strip()
    if custom:
        return custom
    lang = (language or "").lower()
    if lang.startswith("zh") or not lang:
        return AI_LABEL_TEXT
    return AI_LABEL_TEXT_EN


def build_watermark_clip(video_width: int, video_height: int, language: str = ""):
    """Build the persistent corner mark as a MoviePy clip (or None on failure)."""
    try:
        from moviepy import TextClip

        from app.config import config as _config
        from app.utils import utils

        font_name = str(_config.app.get("ai_content_label_font", "") or "").strip()
        if not font_name:
            font_name = str(_config.app.get("font_name", "STHeitiMedium.ttc") or "STHeitiMedium.ttc")
        font_path = os.path.join(utils.font_dir(), font_name)
        if not os.path.isfile(font_path):
            font_path = ""

        size = int(_config.app.get("ai_content_label_font_size", DEFAULT_MARK_FONT_SIZE) or DEFAULT_MARK_FONT_SIZE)
        opacity = float(_config.app.get("ai_content_label_opacity", DEFAULT_MARK_OPACITY) or DEFAULT_MARK_OPACITY)
        text = label_text(language)

        clip = TextClip(
            font=font_path or None,
            text=text,
            font_size=size,
            color="#FFFFFF",
            stroke_color="#000000",
            stroke_width=1,
        )
        clip = clip.with_opacity(max(0.15, min(1.0, opacity)))
        margin = max(12, int(video_height * 0.02))
        position = str(_config.app.get("ai_content_label_position", DEFAULT_MARK_POSITION) or DEFAULT_MARK_POSITION)
        if position == "bottom_left":
            clip = clip.with_position((margin, video_height - clip.h - margin))
        elif position == "top_right":
            clip = clip.with_position((video_width - clip.w - margin, margin))
        elif position == "top_left":
            clip = clip.with_position((margin, margin))
        else:  # bottom_right
            clip = clip.with_position(
                (video_width - clip.w - margin, video_height - clip.h - margin)
            )
        return clip
    except Exception as exc:
        logger.warning(
            f"failed to build AI content watermark: {type(exc).__name__}: {exc}"
        )
        return None


def sidecar_path(video_file: str) -> str:
    """Sidecar sits next to the video with the same stem."""
    stem = os.path.splitext(video_file)[0]
    return f"{stem}.compliance.json"


def _resolve_tts_from_params(params: Any) -> tuple[str, str, bool]:
    """从任务**实际参数**推导 (tts_provider, voice, ai_narration)。

    旧实现直接读全局 ``config.app`` 的 ``tts_provider``/``voice_name``，导致一次
    ``voice_name="no-voice"`` 的静默片被误记成 “provider=edge / voice=女声 /
    已配音”（真实缺陷 #5）。本次任务的音色只存在于 params 里，提供方与是否 AI
    旁白都由音色名路由得出：
    - 上传自定义音频：有旁白但非 AI 合成（provider=custom-audio, narration=False）。
    - 空 / no-voice：无 AI 旁白（provider=none, narration=False）。
    - 具名神经音色：按 voice 路由映射提供方，narration=True。
    """
    from app.services import voice

    voice_name = str(getattr(params, "voice_name", "") or "").strip()
    if getattr(params, "custom_audio_file", None):
        return "custom-audio", "", False
    if not voice_name or voice.is_no_voice(voice_name):
        return "none", voice_name, False
    if voice.is_elevenlabs_voice(voice_name):
        provider = "elevenlabs"
    elif voice.is_fish_audio_voice(voice_name):
        provider = "fish_audio"
    elif voice.is_minimax_voice(voice_name):
        provider = "minimax"
    else:
        provider = "edge"
    return provider, voice_name, True


def _collect_models(tts_override: dict[str, str] | None = None) -> dict[str, Any]:
    """Record which models produced this video.

    ``tts_override`` 由调用方传入从任务实际参数解析出的 TTS 信息；为 None 时
    退回全局配置（保留旧调用方/历史任务的既有行为）。
    """
    from app.config import config as _config

    app = _config.app
    provider = str(app.get("llm_provider", "") or "")
    if tts_override is not None:
        tts = {"provider": tts_override["provider"], "voice": tts_override["voice"]}
    else:
        tts = {
            "provider": str(app.get("tts_provider", "edge") or "edge"),
            "voice": str(app.get("voice_name", "") or ""),
        }
    return {
        "llm": {
            "provider": provider,
            "model": str(app.get(f"{provider}_model_name", "") or ""),
        },
        "tts": tts,
        "subtitle": str(app.get("subtitle_provider", "edge") or "edge"),
        "video_encoder": str(app.get("video_codec", "") or ""),
    }


def _license_summary(sources: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in sources:
        license_name = str(item.get("license") or "unknown").strip() or "unknown"
        counts[license_name] = counts.get(license_name, 0) + 1
    return counts


def write_sidecar(
    video_file: str,
    *,
    task_id: str = "",
    person: str = "",
    material_sources: list[dict[str, Any]] | None = None,
    script: dict[str, Any] | None = None,
    language: str = "",
    params: Any = None,
) -> str:
    """Write <video>.compliance.json. Returns the path, or "" on failure.

    ``params`` 是本次任务的实际参数对象（VideoParams）。传入时，音色 / TTS
    提供方 / 是否 AI 旁白都从中推导；不传时保留旧的全局配置行为。不能
    将静默 no-voice 片错报成已配音。
    """
    try:
        if params is not None:
            eff_provider, eff_voice, narration = _resolve_tts_from_params(params)
            tts_override: dict[str, str] | None = {
                "provider": eff_provider,
                "voice": eff_voice,
            }
        else:
            narration = True
            tts_override = None
        sources = material_sources or []
        timeline = (script or {}).get("timeline") or []
        facts = (script or {}).get("facts_check") or {}
        payload = {
            "schema": "biography.compliance/v1",
            "task_id": task_id,
            "person": person,
            "video_file": Path(video_file).name,
            "ai_generated": {
                "narration": narration,
                "disclosure_label": label_text(language),
                "label_applied": is_enabled(),
            },
            "models": _collect_models(tts_override=tts_override),
            "script": {
                "shot_count": len(timeline),
                "facts_checked": facts.get("checked"),
                "facts_unverified": facts.get("unverified"),
                "unverified_shots": [
                    {
                        "narration": (s.get("narration") or "")[:120],
                        "reasons": s.get("unverified_reasons") or [],
                    }
                    for s in timeline
                    if s.get("verified") is False
                ][:20],
            },
            "sources": [
                {
                    "provider": s.get("provider", ""),
                    "asset_id": s.get("asset_id", ""),
                    "source_page": s.get("source_page", ""),
                    "license": s.get("license", ""),
                    "license_url": s.get("license_url", ""),
                    "creator": (s.get("creator") or {}).get("name", "")
                    if isinstance(s.get("creator"), dict)
                    else str(s.get("creator") or ""),
                    "credit": s.get("credit", ""),
                    "visual_type": s.get("visual_type", ""),
                    "local_file": s.get("local_file", ""),
                }
                for s in sources
            ],
            "license_summary": _license_summary(sources),
            "notes": [
                "旁白由 LLM 撰写并 TTS 合成,画面为公开档案照片,均经授权白名单过滤。",
                "书目仅作事实参考,成片不引用原文;unverified 镜头需人工复核。",
            ],
        }
        path = sidecar_path(video_file)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.success(f"compliance sidecar written: {path}")
        return path
    except Exception as exc:
        logger.warning(f"failed to write compliance sidecar: {type(exc).__name__}: {exc}")
        return ""
