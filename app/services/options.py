"""前端选项数据源（在 app 层复刻，不依赖 streamlit）。

向导的四个下拉——音色 / 视频来源 / 字体 / 语言——其取值逻辑原本散落在
``webui/{base,audio,scripts}.py`` 里，但那些模块依赖 streamlit，app 层不能
import。这里维护与 Streamlit 端语义一致的独立实现，作为 ``/options/*`` 端点
的数据源。字段值（video_source / voice_name / font_name / video_language）与
现有 ``VideoParams``、config.toml、历史任务保持完全兼容。
"""
from __future__ import annotations

import os
from typing import Any

from loguru import logger

from app.config import config
from app.services import voice
from app.utils import utils

# ── 语言（与 webui/tasks.py 的 support_locales 一致）──────────────────────
SUPPORT_LOCALES: tuple[str, ...] = (
    "zh-CN",
    "zh-HK",
    "zh-TW",
    "de-DE",
    "en-US",
    "es-ES",
    "fr-FR",
    "it-IT",
    "ru-RU",
    "vi-VN",
    "th-TH",
    "tr-TR",
)

# 展示名：仅用于下拉文案，取值不进入持久化契约（value 始终为 locale 码）。
_LOCALE_LABELS: dict[str, str] = {
    "zh-CN": "简体中文",
    "zh-HK": "繁體中文（香港）",
    "zh-TW": "繁體中文（台灣）",
    "de-DE": "Deutsch",
    "en-US": "English",
    "es-ES": "Español",
    "fr-FR": "Français",
    "it-IT": "Italiano",
    "ru-RU": "Русский",
    "vi-VN": "Tiếng Việt",
    "th-TH": "ไทย",
    "tr-TR": "Türkçe",
}

# ── 视频来源（与 webui/base.py 的 VIDEO_SOURCE_GROUPS 一致）───────────────
VIDEO_SOURCE_GROUPS: dict[str, tuple[str, ...]] = {
    "stock_video": ("pexels", "pixabay", "coverr"),
    "history_archive": ("historical_archive",),
    "ai_video": (
        "metaso_minimax",
        "ofox",
        "loomloom",
        "volcengine_seedance",
        "wavespeed",
        "muapi",
    ),
    "ai_image": ("openai_image",),
    "local": ("local",),
}

_GROUP_LABELS: dict[str, str] = {
    "stock_video": "库存视频",
    "history_archive": "历史档案",
    "ai_video": "AI 视频",
    "ai_image": "AI 图片",
    "local": "本地素材",
}

# TTS provider（config.ui.tts_server）取值集合，供前端选择器与音色接口共用。
TTS_PROVIDERS: tuple[str, ...] = (
    "azure-tts-v1",
    "azure-tts-v2",
    "siliconflow",
    "gemini-tts",
    "mimo-tts",
    "minimax-tts",
    "elevenlabs",
    "chatterbox",
    "kokoro",
    "fish_audio",
    "voxcpm",
)

DEFAULT_TTS_PROVIDER = "azure-tts-v1"


def active_tts_provider() -> str:
    provider = str(config.ui.get("tts_server", DEFAULT_TTS_PROVIDER) or "").strip()
    return provider if provider in TTS_PROVIDERS else DEFAULT_TTS_PROVIDER


def languages() -> list[dict[str, str]]:
    return [
        {"value": code, "label": _LOCALE_LABELS.get(code, code)}
        for code in SUPPORT_LOCALES
    ]


def video_sources() -> dict[str, Any]:
    """分组 + 扁平两份列表：分组用于下拉 optgroup，扁平用于校验/回退。"""
    groups = [
        {
            "id": group_id,
            "label": _GROUP_LABELS.get(group_id, group_id),
            "sources": [{"value": src, "label": src} for src in sources],
        }
        for group_id, sources in VIDEO_SOURCE_GROUPS.items()
    ]
    flat = [src for sources in VIDEO_SOURCE_GROUPS.values() for src in sources]
    return {"groups": groups, "sources": flat}


def fonts() -> list[dict[str, str]]:
    """扫描 resource/fonts 下的 .ttf/.ttc，排序返回（与 webui.get_all_fonts 一致）。"""
    font_dir = utils.font_dir()
    names: list[str] = []
    try:
        for root, _dirs, files in os.walk(font_dir):
            for file in files:
                if file.endswith(".ttf") or file.endswith(".ttc"):
                    names.append(file)
    except OSError as exc:  # pragma: no cover - 字体目录不可读时降级为空
        logger.warning(f"failed to scan font dir: {exc}")
    names = sorted(set(names))
    return [{"value": name, "label": name} for name in names]


def _friendly_voice_label(tts_provider: str, value: str) -> str:
    """把原始 voice_name 转为展示文案（无 tr，静态替换性别/Neural 后缀）。"""
    if voice.is_no_voice(value):
        return "未选择配音"
    if voice.is_elevenlabs_voice(value):
        parts = value.split(":", 2)
        return parts[2] if len(parts) >= 3 else value
    if voice.is_chatterbox_voice(value) or voice.is_kokoro_voice(value):
        name = value.split(":", 1)[1] if ":" in value else value
        return name.replace("-Female", "").replace("-Male", "")
    if voice.is_minimax_voice(value):
        return value.split(":", 1)[1] if ":" in value else value
    if voice.is_fish_audio_voice(value):
        parts = value.split(":", 2)
        display = parts[2] if len(parts) >= 3 else value
        return display.replace("Female", "女声").replace("Male", "男声")
    if voice.is_voxcpm_voice(value):
        return value.split(":", 1)[1] or voice.VOXCPM_DEFAULT_VOICE
    return (
        value.replace("Female", "女声")
        .replace("Male", "男声")
        .replace("Neural", "")
    )


def _raw_voice_options(tts_provider: str) -> list[str]:
    if tts_provider == "siliconflow":
        return list(voice.get_siliconflow_voices())
    if tts_provider == "gemini-tts":
        return list(voice.get_gemini_voices())
    if tts_provider == "mimo-tts":
        return list(voice.get_mimo_voices())
    if tts_provider == "minimax-tts":
        return list(voice.get_minimax_voices())
    if tts_provider == "elevenlabs":
        return list(voice.get_elevenlabs_voices(voice.get_elevenlabs_api_key()))
    if tts_provider == "chatterbox":
        return list(voice.get_chatterbox_voices())
    if tts_provider == "kokoro":
        return list(voice.get_kokoro_voices())
    if tts_provider == "fish_audio":
        return list(voice.get_fish_audio_voices())
    if tts_provider == "voxcpm":
        return list(voice.get_voxcpm_voices())
    if tts_provider == "azure-tts-v2":
        return [v for v in voice.get_all_azure_voices(filter_locals=None) if "V2" in v]
    if tts_provider == "azure-tts-v1":
        return [
            v for v in voice.get_all_azure_voices(filter_locals=None) if "V2" not in v
        ]
    # 未知 provider：按默认（azure v1）返回，避免前端拿到空列表卡住向导。
    return [
        v for v in voice.get_all_azure_voices(filter_locals=None) if "V2" not in v
    ]


def voices(provider: str | None = None) -> dict[str, Any]:
    """按 provider 返回音色列表；provider 为空时取当前配置的活动 provider。"""
    selected = (
        provider.strip()
        if provider and provider.strip() in TTS_PROVIDERS
        else active_tts_provider()
    )
    options: list[dict[str, str]] = []
    try:
        options = [
            {"value": raw, "label": _friendly_voice_label(selected, raw)}
            for raw in _raw_voice_options(selected)
        ]
    except Exception as exc:  # pragma: no cover - 远端音色接口失败不应 500
        logger.warning(f"failed to load voices for {selected}: {exc}")
    return {"provider": selected, "providers": list(TTS_PROVIDERS), "voices": options}
