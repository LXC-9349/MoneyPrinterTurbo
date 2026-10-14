# webui/base.py — 基础层: i18n / 控件 / 常量 / 运行时配置 / 会话初始化
# 依赖底座, 被 tasks/audio/scripts/video/subtitles/generation 全部 import.

import hashlib
import html
import json
import math
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import time
import webbrowser
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import requests
import streamlit as st
from loguru import logger

# WebUI 作为独立入口运行时，需要让项目根目录优先于第三方依赖，
# 避免依赖中的同名 app 包遮蔽 MoneyPrinterTurbo 自己的 app 包。
root_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if root_dir in sys.path:
    sys.path.remove(root_dir)
sys.path.insert(0, root_dir)

from app.config import config
from app.models import const
from app.models.llm_provider import (
    DEFAULT_LLM_PROVIDER_ID,
    LLM_PROVIDER_REGISTRY,
    get_llm_provider,
    normalize_provider_override,
)
from app.models.schema import (
    MaterialInfo,
    VideoAspect,
    VideoConcatMode,
    VideoFitMode,
    VideoParams,
    VideoTransitionMode,
)
from app.services import bgm as bgm_service
from app.services import (
    biography,
    cache_manager,
    llm,
    loomloom,
    material,
    metaso_minimax,
    muapi,
    ofox,
    video,
    volcengine_seedance,
    voice,
    webui_task,
)
from app.services import elevenlabs_music as elevenlabs_music_service
from app.services import sonilo as sonilo_service
from app.services import state as sm
from app.services import task as tm
from app.services import version_checker
from app.utils.logging_utils import configure_terminal_logger
from app.utils import utils



st.set_page_config(
    page_title="MoneyPrinterTurbo",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={
        "Report a bug": "https://github.com/harry0703/MoneyPrinterTurbo/issues",
        "About": "# MoneyPrinterTurbo\nSimply provide a topic or keyword for a video, and it will "
        "automatically generate the video copy, video materials, video subtitles, "
        "and video background music before synthesizing a high-definition short "
        "video.\n\nhttps://github.com/harry0703/MoneyPrinterTurbo",
    },
)


# Streamlit 1.59 会在页面右上角默认展示 Deploy、skills nudge 等平台入口。
# MoneyPrinterTurbo 是面向终端用户的本地工具,这些入口会造成顶部大块空白,
# 也会让新用户误以为需要安装额外组件。这里统一隐藏 Streamlit 平台工具栏,
# 并压缩主容器顶部留白,只保留项目自己的标题、语言选择和业务设置区域。
#
# 注意:样式必须在**脚本运行时**注入,不能在 import 期调用 st.markdown。
# webui/base.py 的顶层代码在 Main.py 顶部被 import 时执行一次,那时 Streamlit
# 的渲染树尚未建立,注入的 <style> 会被丢弃(整份 styles.css 因而从未生效)。
# 改为渲染期函数,由 Main._render_application() 每轮调用。
_style_file = Path(__file__).with_name("styles.css")


def inject_styles() -> None:
    """把 styles.css 注入当前页面。必须在脚本运行期调用(每轮 rerun)。"""
    try:
        streamlit_style = f"<style>{_style_file.read_text(encoding='utf-8')}</style>"
        st.markdown(streamlit_style, unsafe_allow_html=True)
    except Exception as exc:  # pragma: no cover - 样式失败不应阻断页面
        logger.debug(f"failed to inject styles.css: {exc}")
# 定义资源目录
font_dir = os.path.join(root_dir, "resource", "fonts")
song_dir = os.path.join(root_dir, "resource", "songs")
i18n_dir = os.path.join(root_dir, "webui", "i18n")
config_file = os.path.join(root_dir, "webui", ".streamlit", "webui.toml")
# 语言列表必须在会话状态初始化前可用，首次访问时才能把浏览器 locale 映射到
# 项目真正支持的语言；自动识别结果只进入当前会话，不修改全局配置。

locales = utils.load_locales(i18n_dir)
DEFAULT_CHATTERBOX_BASE_URL = "http://127.0.0.1:4123/v1"
DEFAULT_CHATTERBOX_MODEL = "chatterbox"
DEFAULT_CHATTERBOX_VOICES = ["default-Female"]
DEFAULT_KOKORO_BASE_URL = "http://127.0.0.1:8880/v1"
DEFAULT_KOKORO_MODEL = "kokoro"
# empty = ask the server for its voice list (GET {base_url}/audio/voices)
DEFAULT_KOKORO_VOICES: list[str] = []
DEFAULT_VOXCPM_BASE_URL = voice.VOXCPM_DEFAULT_BASE_URL
DEFAULT_VOXCPM_VOICE = voice.VOXCPM_DEFAULT_VOICE
CUSTOM_LLM_ENDPOINT_ID = "custom"
VOICE_MODE_TTS = "tts"
VOICE_MODE_UPLOAD = "upload"
VOICE_MODE_NONE = "none"
LOOMLOOM_MAX_POLL_FAILURES = 5
# WebUI 按素材能力分组展示视频来源，但底层仍保存原有 video_source 值。
# AI 视频组与设置页共用同一业务顺序：合作服务商优先，并按秘塔、OFox、
# 胜算云、火山引擎排列；其余服务随后展示。这样两个入口的顺序一致，同时
# 不改变 config.toml、历史任务和 API 请求中的字段语义，旧用户无需迁移配置。
VIDEO_SOURCE_GROUPS = {
    "stock_video": ("pexels", "pixabay", "coverr"),
    # 传记素材源:抓取人物真实历史照片(Commons/Wikipedia/LOC),
    # 与库存/AI 视频源并列展示,不需要 API key。
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

# 传记视频素材源。作为独立的“生成模式”（普通/传记）使用，
# 后端与素材管道仍按 video_source == 历史档案 判断，无需改动现有链路。
_BIOGRAPHY_SOURCE = "historical_archive"
_NORMAL_SOURCES = [
    source
    for group in ("stock_video", "ai_video", "ai_image", "local")
    for source in VIDEO_SOURCE_GROUPS[group]
]


def _current_normal_source():
    """普通视频模式下的素材源：读取“视频来源”下拉当前值；
    无效或仍是传记源时退回配置里的源，再无效则 pexels。"""
    ws = st.session_state.get(localized_widget_key("video_source_select"))
    ws = str(ws).strip() if ws is not None else ""
    if ws not in _NORMAL_SOURCES:
        ws = str(config.app.get("video_source", "pexels") or "pexels")
    return ws if ws in _NORMAL_SOURCES else "pexels"


def _render_generation_mode():
    """页面顶部的“普通视频 / 传记视频”模式切换，返回所选模式。

    传记模式把素材来源固定为历史档案（传记视频从档案抓取该人物真实照片）；
    普通模式沿用“视频设置”里的“视频来源”下拉选择。生成模式与具体素材源
    解耦，避免把“传记”这种生成模式塞进素材来源列表。"""
    default_mode = st.session_state.get("generation_mode")
    if default_mode not in ("normal", "biography"):
        default_mode = (
            "biography"
            if str(config.app.get("video_source", "")).strip() == _BIOGRAPHY_SOURCE
            else "normal"
        )
    choices = [(tr("Normal Video"), "normal"), (tr("Biography Video"), "biography")]
    label_map = {value: label for label, value in choices}
    selected = stable_selectbox(
        tr("Generation Mode"),
        options=["normal", "biography"],
        default_value=default_mode,
        key="generation_mode_select",
        help=tr("Generation Mode Help"),
        format_func=lambda value: label_map.get(value, value),
    )
    st.session_state["generation_mode"] = selected
    _set_runtime_config("app", "generation_mode", selected)
    if selected == "biography":
        _set_runtime_config("app", "video_source", _BIOGRAPHY_SOURCE)
    else:
        _set_runtime_config("app", "video_source", _current_normal_source())
    return selected

# Upload-Post 的 API Key 与发布用户分别在两个页面管理，并且发布用户名称
# 不等于登录邮箱。集中维护入口可以避免多语言文案各自硬编码 URL 后发生偏差，
# 也方便用户从 WebUI 直接完成首次配置和后续账号维护。
UPLOAD_POST_API_KEYS_URL = "https://app.upload-post.com/api-keys"
UPLOAD_POST_MANAGE_USERS_URL = "https://app.upload-post.com/manage-users"
# 素材设置与视频来源说明共用推广入口，避免两个位置的链接参数不一致。
OFOX_REFERRAL_URL = (
    "https://ofox.ai/?utm_source=github"
    "&utm_medium=sponsorship&utm_content=moneyprinterturbo"
)
# “默认”是 WebUI 专用哨兵，不会写入 config.toml，也不会传给 FFmpeg。
# 后端在 video_codec 未配置时继续采用稳定的 libx264；单独保留该哨兵可以区分
# “跟随项目默认策略”和“用户明确固定 libx264”，便于未来安全调整默认策略。
DEFAULT_VIDEO_CODEC_OPTION = "__default__"
# LoomLoom 的能力接口只返回模型 ID 和展示名，不提供价格。这里仅维护用户确认过
# 的参考价，用于帮助选择模型；最终费用按实际模型调用结算。别名同时覆盖
# 当前展示名和常见模型 ID，未收录的新模型会自然返回空价格，不影响选择或报价。
LOOMLOOM_VIDEO_MODEL_PRICES = (
    (("veo31fast", "googleveo31fastpreview"), "￥0.700/秒", "￥0.700/秒"),
    (
        (
            "通义万相22图生视频fastlora",
            "通义万相22文生视频fastlora",
            "tongyiwanxiang22i2vfastlora",
            "tongyiwanxiang22t2vfastlora",
            "wanx22i2vfastlora",
            "wanx22t2vfastlora",
        ),
        "￥0.350–0.770/条",
        "￥0.350/条（480P）；￥0.770/条（720P）",
    ),
    (("即梦30文生视频720p", "jimeng30t2v720p"), "￥0.230/秒", "￥0.230/秒"),
    (("即梦30pro视频", "jimeng30pro视频", "jimeng30provideo"), "￥1.000/秒", "￥1.000/秒"),
    (("veo3", "googleveo3"), "￥1.400/秒", "￥1.400/秒"),
    (("veo31", "googleveo31"), "￥1.400/秒", "￥1.400/秒"),
    (
        ("klingv2", "可灵v2"),
        "￥10.00–20.00/条",
        "￥10.00/条（5 秒）；￥20.00/条（10 秒）",
    ),
    (
        ("klingv21master", "可灵v21master"),
        "￥10.00–20.00/条",
        "￥10.00/条（5 秒）；￥20.00/条（10 秒）",
    ),
    (
        ("viduq3pro",),
        "￥0.440–1.000/秒",
        "￥0.440/秒（540P）；￥0.940/秒（720P）；￥1.000/秒（1080P）",
    ),
)
DEFAULT_SUBTITLE_SETTINGS = {
    "subtitle_enabled": True,
    "font_name": "MicrosoftYaHeiBold.ttc",
    "subtitle_position": "bottom",
    "subtitle_display_mode": "sentence",
    "subtitle_animation": "none",
    "custom_position": 70.0,
    "text_fore_color": "#FFFFFF",
    "font_size": 60,
    "stroke_color": "#000000",
    "stroke_width": 1.5,
    "subtitle_background_enabled": False,
    "subtitle_background_color": "#000000",
    "rounded_subtitle_background": False,
}
LOCAL_MATERIAL_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".avi",
    ".flv",
    ".mkv",
    ".jpg",
    ".jpeg",
    ".png",
}
CUSTOM_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
_FINAL_VIDEO_PATTERN = re.compile(
    r"^final-(?P<index>\d+)\.(?P<extension>mp4|mov|mkv|webm)$",
    re.IGNORECASE,
)
_DOWNLOAD_FILENAME_INVALID_PATTERN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_FILENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {
        f"{prefix}{number}"
        for prefix in ("COM", "LPT")
        for number in range(1, 10)
    }
    # Win32 还会把 Latin-1 上标数字 ¹、²、³ 识别为设备编号。虽然这类主题
    # 很少见，但仍会导致 Windows 下载失败，因此与普通数字保留名统一处理。
    | {
        f"{prefix}{number}"
        for prefix in ("COM", "LPT")
        for number in ("¹", "²", "³")
    }
)
_RUNTIME_CONFIG_SECTION_NAMES = (
    "app",
    "azure",
    "chatterbox",
    "kokoro",
    "elevenlabs",
    "minimax_tts",
    "siliconflow",
    "fish_audio",
    "voxcpm",
    "ui",
)


class _RuntimeConfigSections(Mapping):
    """运行期配置分区视图。

    原单文件脚本每轮运行都会重新执行 ``{"app": config.app, ...}``,因此运行期
    替换 ``config.app``(例如测试 patch 或多个页面)后立即生效。拆分为模块后,
    模块级快照只会在首次 import 时建立,后续 ``config.<section>`` 的替换不会被
    看到,导致写入落到旧对象。这里改为每次访问都按名字到 ``config`` 上实时解析,
    既保留原 dict-like 接口,又与原脚本语义一致。
    """

    def __getitem__(self, name):
        if name not in _RUNTIME_CONFIG_SECTION_NAMES:
            raise KeyError(name)
        return getattr(config, name)

    def __iter__(self):
        return iter(_RUNTIME_CONFIG_SECTION_NAMES)

    def __len__(self):
        return len(_RUNTIME_CONFIG_SECTION_NAMES)

    def get(self, name, default=None):
        try:
            return self[name]
        except (KeyError, AttributeError):
            return default


_RUNTIME_CONFIG_SECTIONS = _RuntimeConfigSections()
# 设置预设与密钥备份使用各自的文件标识。导入时先校验 schema 和版本，
# 避免把任务记录、config.toml 或其它 JSON 误当成本功能的导出文件。
SETTINGS_PRESET_SCHEMA = "moneyprinterturbo.settings-preset"
SETTINGS_PRESET_VERSION = 1
SETTINGS_PRESET_FILE_NAME = "moneyprinterturbo-settings.json"
KEY_BACKUP_SCHEMA = "moneyprinterturbo.key-backup"
KEY_BACKUP_VERSION = 1
KEY_BACKUP_FILE_NAME = "moneyprinterturbo-keys.json"
# 预设只描述生成参数。素材、配音和配乐都是本机文件路径，预设通常要在另一台
# 机器或另一个容器里导入，带上这些路径只会指向不存在的文件。
PRESET_EXCLUDED_PARAM_KEYS = frozenset(
    {
        "video_materials",
        "custom_audio_file",
        "bgm_file",
    }
)
# 密钥按配置项名称后缀识别。新增 Provider 只要沿用现有命名，就会自动进入
# 备份，不需要再维护第二份密钥清单。
CREDENTIAL_KEY_SUFFIXES = (
    "api_key",
    "api_keys",
    "api_token",
    "access_key",
    "secret_key",
    "speech_key",
)
# 只恢复密钥而不恢复配套配置项时，凭据仍然不可用。这些配套项与密钥一起备份。
CREDENTIAL_COMPANION_KEYS = {
    # Azure 语音必须同时知道区域。
    "azure": ("speech_region",),
    # Provider 的额外字段由 Registry 声明，例如 Cloudflare AI Gateway 的
    # Account ID 和 Gateway ID。只恢复 API Key 而丢掉这些字段时，换到另一台
    # 机器后该 Provider 仍然无法调用。从 Registry 读取可以让以后新增的
    # Provider 自动进入备份，不需要在这里维护第二份字段清单。
    "app": tuple(
        provider.config_key(field.config_suffix)
        for provider in LLM_PROVIDER_REGISTRY
        for field in provider.extra_fields
    ),
}

NON_LLM_COMPANION_KEYS = {
    "app": ("upload_post_username",)
}
# 同一个密钥在不同面板可能使用各自的控件 key：音频面板直接编辑 Gemini 和
# MiMo 的 LLM 密钥。恢复备份时必须清除每一个别名，否则遗留的旧值
# 会在下一次 rerun 覆盖刚刚恢复的密钥。
CREDENTIAL_WIDGET_STATE_ALIASES = {
    ("app", "gemini_api_key"): ("gemini_tts_api_key_input",),
    ("app", "mimo_api_key"): ("mimo_tts_api_key_input",),
}
# ui 分区只保存界面偏好，不含任何凭据，备份时整体跳过。
KEY_BACKUP_EXCLUDED_SECTIONS = frozenset({"ui"})

def _is_llm_configured(app_config: Mapping | None = None) -> bool:
    """判断「当前 LLM 是否已配置可用 Key」。

    只读取 app 区段的 LLM 密钥字段（config_key("api_key") / access_key /
    api_token / secret_key / speech_key），任一非空即视为已配置。这里
    不要求 provider 有效或网络可达，目的仅是区分「首次新用户」与
    「已配好 Key 的成熟用户」——前者才值得轻量引导，后者应零打扰。
    返回 bool，供 _render_first_run_onboarding 做条件渲染；失败时按未配置
    处理以保守避免漏提示。
    """
    section = app_config if app_config is not None else config.app
    if not isinstance(section, Mapping):
        return False
    try:
        for provider in LLM_PROVIDER_REGISTRY:
            spec = get_llm_provider(provider.provider_id)
            if spec is None:
                continue
            for suffix in (
                "api_key", "api_keys", "api_token", "secret_key", "speech_key",
            ):
                key = section.get(spec.config_key(suffix))
                if key and str(key).strip():
                    return True
    except Exception as exc:  # pragma: no cover
        logger.debug(f"failed to check llm configured: {exc}")
    return False

def _set_runtime_config(section_name, key, value):
    """
    更新 WebUI 配置，但不等待正在生成视频的后台任务。

    后台任务结束前，配置层只保留同一配置项的最新值；任务释放配置锁时会自动
    应用并保存。页面控件值仍由 Streamlit session_state 维护，因此暂存期间的
    rerun 不会把用户刚输入的内容重置为旧配置。
    """
    config_section = _RUNTIME_CONFIG_SECTIONS[section_name]
    updated = config.update_config_nonblocking(config_section, key, value)
    if not updated:
        logger.debug(f"deferred WebUI config update: section={section_name}, key={key}")
    return updated


def _delete_runtime_config(section_name, key):
    """删除 WebUI 配置项；后台任务占用配置时延后执行。"""
    config_section = _RUNTIME_CONFIG_SECTIONS[section_name]
    deleted = config.delete_config_nonblocking(config_section, key)
    if not deleted:
        logger.debug(f"deferred WebUI config delete: section={section_name}, key={key}")
    return deleted


def _save_runtime_config():
    """请求保存 WebUI 配置；后台任务占用配置时立即返回。"""
    saved = config.try_save_config()
    if not saved:
        logger.debug("deferred WebUI config save until active task completes")
    return saved


def _saved_ui_choice(key, options, default):
    """读取一个持久化选择，并把旧配置或手工编辑的非法值降级为默认值。"""
    options = list(options)
    saved = config.ui.get(key, default)
    numeric_default = isinstance(default, (int, float)) and not isinstance(
        default, bool
    )
    # bool 是 int 的子类，``True == 1``。手工把数值选项写成 TOML
    # 布尔值时必须拒绝，不能让它伪装成第一个数值 option。
    if numeric_default and isinstance(saved, bool):
        return default
    for option in options:
        if saved == option:
            # 返回 options 中的真实值，顺便把 TOML 1.0 等价归一化为
            # 整数选项 1，避免下游参数类型随配置写法漂移。
            return option

    # TOML 中的数值通常保留原类型；仍兼容用户手工写成字符串的情况。
    if numeric_default and isinstance(saved, str):
        try:
            converted = type(default)(saved)
        except (TypeError, ValueError):
            converted = None
        for option in options:
            if converted == option:
                return option
    return default


def _saved_ui_number(key, default, minimum, maximum, number_type=float):
    """读取并限幅持久化数值，避免非法配置破坏 Streamlit slider。"""
    try:
        saved = config.ui.get(key, default)
        if isinstance(saved, bool):
            raise ValueError("boolean is not a numeric setting")
        value = number_type(saved)
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("non-finite value")
    except (TypeError, ValueError, OverflowError):
        value = default
    return min(maximum, max(minimum, value))


def _saved_ui_bool(key, default):
    """兼容 TOML 布尔值和常见手工字符串，拒绝含义不明的旧值。"""
    value = config.ui.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _saved_ui_color(key, default):
    """只把标准六位十六进制颜色传给 Streamlit color picker。"""
    value = str(config.ui.get(key, default) or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        return value
    return default


def _saved_ui_text(key, default="", max_length=None):
    """读取持久化文本并遵守对应 WebUI 控件的长度上限。"""
    value = str(config.ui.get(key, default) or default)
    if max_length is not None:
        value = value[:max_length]
    return value

def _run_llm_read_operation(operation_name, operation):
    """
    使用稳定的当前 LLM 配置执行只读请求，并避免等待视频生成任务。

    能立即取得配置锁时继续沿用原来的互斥保护；锁已被后台视频任务持有时，
    全局配置在任务结束前不会发生变化，因此可以安全复制当前配置，并叠加页面
    尚未落盘的 Provider、模型和密钥。这样新文案使用界面中的最新选择，同时
    不会改变正在生成的视频任务。
    """
    with config.try_runtime_config_lock() as lock_acquired:
        # 配置层在复制全局值和叠加待更新值期间持有队列锁，因此快照只能看到
        # 更新前或更新后的完整状态，不会混用两组 Provider 参数。
        app_config_snapshot = config.snapshot_config_with_pending(config.app)
        if lock_acquired:
            return operation(app_config_snapshot)

    logger.info(
        f"run read-only LLM operation with active task configuration: "
        f"operation={operation_name}"
    )
    return operation(app_config_snapshot)

def get_all_fonts():
    # 字体目录很少变化，但 Streamlit 每次控件交互都会 rerun 页面。短周期缓存
    # 可以避免连续重复 os.walk，同时保证新增字体后最多 30 秒即可被发现。
    fonts = []
    for root, dirs, files in os.walk(font_dir):
        for file in files:
            if file.endswith(".ttf") or file.endswith(".ttc"):
                fonts.append(file)
    fonts.sort()
    return fonts


@st.cache_data(ttl=30, show_spinner=False)
def get_all_songs():
    # 背景音乐与字体使用相同的短周期策略，不做永久缓存，兼顾 rerun 性能和
    # 用户运行期间手动添加音乐文件的场景。
    songs = []
    for root, dirs, files in os.walk(song_dir):
        for file in files:
            if file.endswith(".mp3"):
                songs.append(file)
    return songs


def open_task_folder(task_id):
    try:
        # task_id 应始终是服务端生成的 UUID。这里先做格式校验，避免异常值
        # 通过路径拼接访问任务目录之外的位置，也避免后续打开目录时触发
        # 平台 shell 对特殊字符的解释。
        normalized_task_id = str(UUID(str(task_id)))
        tasks_root = os.path.abspath(os.path.join(root_dir, "storage", "tasks"))
        path = os.path.abspath(os.path.join(tasks_root, normalized_task_id))

        # 即使 UUID 校验通过，也再次确认最终路径仍在任务根目录内，避免
        # 未来调用方调整 task_id 来源时引入路径穿越风险。
        if not path.startswith(tasks_root + os.sep):
            logger.warning(f"invalid task folder path: {path}")
            return

        if os.path.isdir(path):
            webbrowser.open(f"file://{path}")
    except Exception as e:
        logger.exception(f"failed to open task folder: task_id={task_id}, error={e}")



@st.cache_resource
def init_log():
    # 基础日志 Handler 属于进程级资源，而不是页面会话状态。Streamlit 每次组件
    # 交互都会 rerun 页面脚本，代码热重载也可能让缓存失效。日志初始化只能
    # 精确替换终端 Handler，不能清空正在生成任务使用的 WebUI 临时 Handler。
    _lvl = "DEBUG"

    return configure_terminal_logger(
        sys.stdout,
        level=_lvl,
        colorize=True,
    )


def _current_ui_language():
    """当前界面语言;会话未初始化时(如模块 import 期求值 dialog 装饰器标题)
    回退到已保存的配置语言,避免 st.session_state 缺键直接抛 KeyError。"""
    language = st.session_state.get("ui_language")
    if language:
        return language
    return config.ui.get("language", "") or st.session_state.get("_ui_language_default", "")


def tr(key):
    loc = locales.get(_current_ui_language(), {})
    value = loc.get("Translation", {}).get(key)
    if value is not None:
        return value
    # 新功能优先维护中英文。其它语言缺少单项翻译时统一回退英文，避免在多个
    # locale 中复制相同英文后长期失去同步；英文也没有该键时才显示原始 key。
    return locales.get("en", {}).get("Translation", {}).get(key, key)


def tr_optional(key, fallback_language=""):
    loc = locales.get(_current_ui_language(), {})
    value = loc.get("Translation", {}).get(key, "")
    if not value and fallback_language:
        fallback_loc = locales.get(fallback_language, {})
        value = fallback_loc.get("Translation", {}).get(key, "")
    return value if value else ""

def localized_widget_key(name, *parts):
    # 部分 Streamlit selectbox 使用稳定 key 记住选择状态，但展示文本来自 locale。
    # 语言切换时把语言也放进 key，可以强制重建控件，避免选中项仍显示旧语言。
    language = st.session_state.get("ui_language", config.ui.get("language", ""))
    suffix_parts = [name, language, *[str(part) for part in parts if part]]
    return "_".join(suffix_parts)

def stable_selectbox(label, options, default_value, key, format_func=None, **kwargs):
    # Streamlit 1.59 对 selectbox 的状态复用更敏感：如果控件没有固定 key，
    # 或者真实选项只是一组临时下标，页面 rerun 后容易被重新计算的 index 覆盖，
    # 表现为用户第一次选择不生效、需要再选一次。这个 helper 统一用稳定业务值
    # 作为真实选项，并在 session_state 里保存该值；展示文案只通过 format_func
    # 转换，避免翻译文案、选项顺序或上游配置变化影响选择状态。
    options = list(options)
    if not options:
        raise ValueError(f"selectbox options cannot be empty: {key}")

    if default_value not in options:
        default_value = options[0]

    widget_key = localized_widget_key(key)
    selected_value = st.session_state.get(widget_key)
    accepts_custom_value = bool(kwargs.get("accept_new_options"))
    has_valid_custom_value = (
        accepts_custom_value
        and isinstance(selected_value, str)
        and bool(selected_value.strip())
    )
    if selected_value not in options and not has_valid_custom_value:
        # 如果上游选项发生变化（例如切换 TTS provider 后声音列表变了），
        # 旧值已经不合法。控件创建前直接初始化 session_state，之后只让 key
        # 管理状态，不再同时传入 index。这样可以避免 Streamlit 在 rerun 时
        # 用重新计算的 index 覆盖用户刚选择的值，导致第一次选择不生效。
        st.session_state[widget_key] = default_value

    if format_func is None:
        format_func = str

    return st.selectbox(
        label,
        options=options,
        format_func=format_func,
        key=widget_key,
        **kwargs,
    )


# Streamlit 原生 selectbox 暂不支持 HTML optgroup。这里使用 1.59 自带的
# Components v2 封装原生 <select>/<optgroup>，无需引入前端依赖，同时保留浏览器
# 原生的键盘导航、无障碍语义和移动端选择体验。组件只传递固定业务值和翻译文本，
# 不接收任意 HTML，从边界上避免配置内容进入 innerHTML。
_GROUPED_SELECT_HTML = """
        <div class="mpt-grouped-select">
            <div class="mpt-grouped-select__label-row">
                <label class="mpt-grouped-select__label"></label>
                <button class="mpt-grouped-select__settings" type="button"></button>
            </div>
            <div class="mpt-grouped-select__control">
                <select></select>
            </div>
        </div>
    """

_GROUPED_SELECT_CSS = """
        .mpt-grouped-select {
            width: 100%;
            color: var(--st-text-color);
            font-family: var(--st-font);
        }

        .mpt-grouped-select__label-row {
            display: flex;
            flex-wrap: wrap;
            align-items: baseline;
            gap: 0.45rem;
            margin-bottom: 0.35rem;
        }

        .mpt-grouped-select__label {
            font-size: 0.875rem;
            line-height: 1.25rem;
        }

        .mpt-grouped-select__settings {
            padding: 0;
            border: 0;
            background: transparent;
            color: var(--st-link-color);
            font: inherit;
            font-size: 0.8rem;
            line-height: 1.25rem;
            cursor: pointer;
        }

        .mpt-grouped-select__settings:hover {
            text-decoration: underline;
            text-underline-offset: 0.15rem;
        }

        .mpt-grouped-select__settings:focus-visible {
            border-radius: 0.2rem;
            outline: 2px solid var(--st-primary-color);
            outline-offset: 2px;
        }

        .mpt-grouped-select__control {
            position: relative;
        }

        .mpt-grouped-select__control::after {
            position: absolute;
            top: 50%;
            right: 1rem;
            width: 0.55rem;
            height: 0.55rem;
            border-right: 2px solid currentColor;
            border-bottom: 2px solid currentColor;
            content: "";
            pointer-events: none;
            transform: translateY(-70%) rotate(45deg);
        }

        .mpt-grouped-select select {
            width: 100%;
            min-height: 2.5rem;
            padding: 0.45rem 2.75rem 0.45rem 0.75rem;
            border: 1px solid color-mix(in srgb, currentColor 20%, transparent);
            border-radius: 0.5rem;
            outline: none;
            appearance: none;
            background: var(--st-secondary-background-color);
            color: inherit;
            font: inherit;
            cursor: pointer;
        }

        .mpt-grouped-select select:hover {
            border-color: color-mix(in srgb, currentColor 36%, transparent);
        }

        .mpt-grouped-select select:focus-visible {
            border-color: var(--st-primary-color);
            box-shadow: 0 0 0 1px var(--st-primary-color);
        }
    """

_GROUPED_SELECT_JS = """
        export default function(component) {
            const { data, parentElement, setTriggerValue } = component;
            const label = parentElement.querySelector("label");
            const settings = parentElement.querySelector(".mpt-grouped-select__settings");
            const select = parentElement.querySelector("select");

            label.textContent = data.label;
            settings.textContent = data.settingsLabel;
            settings.hidden = !data.settingsLabel;
            select.id = data.controlId;
            label.htmlFor = data.controlId;
            select.setAttribute("aria-label", data.label);
            select.replaceChildren();

            for (const groupData of data.groups) {
                const group = document.createElement("optgroup");
                group.label = groupData.label;
                for (const optionData of groupData.options) {
                    const option = document.createElement("option");
                    option.value = optionData.value;
                    option.textContent = optionData.label;
                    group.appendChild(option);
                }
                select.appendChild(group);
            }

            select.value = data.value;
            const handleChange = () => {
                setTriggerValue("selected", select.value);
            };
            const handleSettings = () => {
                setTriggerValue("settings", true);
            };
            select.addEventListener("change", handleChange);
            settings.addEventListener("click", handleSettings);

            return () => {
                select.removeEventListener("change", handleChange);
                settings.removeEventListener("click", handleSettings);
            };
        }
    """


# 组件注册表挂在 Streamlit Runtime 上,而本模块作为包只会被 import 一次:模块级
# 的 st.components.v2.component(...) 只在首次 import 时执行,那时通常还没有
# Runtime,定义被注册进临时管理器,之后每次脚本运行都报 "component is not
# registered"。拆分前的单文件脚本每次运行都会重新执行该声明,因此这里把注册
# 延后到渲染期,按当前 Runtime 重新注册,保持与拆分前一致的语义。
# 缓存以 Runtime 实例作 weakref key，而非 id(Runtime)：AppTest 会频繁
# 创建/销毁 Runtime，id 在回收后被新 Runtime 复用时旧 mount 会被误认为
# 有效→ "Component is not registered"；WeakKeyDictionary 在 Runtime 消亡后
# 自然失效，不会跨 Runtime 递送错误的 mount。
from weakref import WeakKeyDictionary

_GROUPED_SELECT_COMPONENT_CACHE: "WeakKeyDictionary" = WeakKeyDictionary()


def _get_grouped_select_component():
    """按当前 Runtime 返回已注册的分组下拉组件挂载函数(必要时重新注册)。"""
    from streamlit.runtime import Runtime

    if not Runtime.exists():
        # 无 Runtime（开发时 import 期求值等）→ 直接注册并返回 mount，不进缓存。
        return st.components.v2.component(
            "mpt_grouped_select",
            html=_GROUPED_SELECT_HTML,
            css=_GROUPED_SELECT_CSS,
            js=_GROUPED_SELECT_JS,
        )
    runtime = Runtime.instance()
    mount = _GROUPED_SELECT_COMPONENT_CACHE.get(runtime)
    if mount is None:
        mount = st.components.v2.component(
            "mpt_grouped_select",
            html=_GROUPED_SELECT_HTML,
            css=_GROUPED_SELECT_CSS,
            js=_GROUPED_SELECT_JS,
        )
        try:
            _GROUPED_SELECT_COMPONENT_CACHE[runtime] = mount
        except TypeError:  # pragma: no cover - Runtime 不支持 weakref 时不缓存
            pass
    return mount



def grouped_selectbox(
    label,
    groups,
    default_value,
    key,
    format_func=None,
    settings_label="",
    on_settings=None,
):
    """渲染带不可选分组标题的单个下拉框，并返回稳定业务值。"""
    if format_func is None:
        format_func = str

    normalized_groups = []
    valid_values = []
    for group_label, options in groups:
        normalized_options = []
        for option in options:
            valid_values.append(option)
            normalized_options.append(
                {"value": option, "label": str(format_func(option))}
            )
        if normalized_options:
            normalized_groups.append(
                {"label": str(group_label), "options": normalized_options}
            )

    if not valid_values:
        raise ValueError(f"grouped selectbox options cannot be empty: {key}")
    if len(set(valid_values)) != len(valid_values):
        raise ValueError(f"grouped selectbox options must be unique: {key}")
    if default_value not in valid_values:
        default_value = valid_values[0]

    # 业务选择保存在与旧 selectbox 相同的 session key 中，设置预设恢复和
    # 语言切换逻辑无需分叉；组件自身使用独立 key，避免与业务状态冲突。
    widget_key = localized_widget_key(key)
    if widget_key not in st.session_state:
        st.session_state[widget_key] = default_value
    selected_value = st.session_state[widget_key]
    if selected_value not in valid_values:
        selected_value = default_value
        st.session_state[widget_key] = selected_value

    result = _get_grouped_select_component()(
        key=f"{widget_key}_component",
        data={
            "label": label,
            "settingsLabel": settings_label,
            # 显式关联可见 label 与原生 select。组件 key 由固定业务名称和
            # 语言代码组成，在页面内唯一，既方便鼠标点击标签聚焦控件，
            # 也不会引入随机 ID 导致每次 rerun 都重建前端状态。
            "controlId": f"{widget_key}_control",
            "value": selected_value,
            "groups": normalized_groups,
        },
        on_selected_change=lambda: None,
        on_settings_change=on_settings or (lambda: None),
    )
    changed_value = getattr(result, "selected", None)
    if changed_value in valid_values and changed_value != selected_value:
        st.session_state[widget_key] = changed_value
        # Components v2 在当前脚本轮次返回事件时，本轮传给前端的 data 仍是
        # 事件发生前的旧值。立即自动 rerun，让组件和依赖 video_source 的控件
        # 同时收到新值；否则下拉框会被旧 data 短暂覆盖，用户只能再选一次。
        st.rerun()

    return selected_value



def _initialize_session_state():
    """集中初始化跨 rerun 保留的页面状态。"""
    if not st.session_state.get("cross_post_recovery_checked"):
        # WebUI 可以不经过 FastAPI 独立运行，因此也需要在首次会话初始化时处理
        # 进程重启留下的发布状态。恢复失败时不写标记，后续 rerun 会再次尝试。
        recovered = tm.recover_interrupted_cross_posts()
        if recovered is not None:
            st.session_state["cross_post_recovery_checked"] = True

    saved_ui_language = config.ui.get("language", "")
    browser_locale = st.context.locale
    initial_ui_language = utils.resolve_ui_language(
        saved_language=saved_ui_language,
        browser_locale=browser_locale,
        supported_languages=locales.keys(),
    )

    defaults = {
        "video_subject": "",
        "video_script": "",
        "video_terms": "",
        "paragraph_number_input": _saved_ui_number(
            "paragraph_number",
            1,
            llm.MIN_SCRIPT_PARAGRAPH_NUMBER,
            llm.MAX_SCRIPT_PARAGRAPH_NUMBER,
            int,
        ),
        "video_script_prompt": _saved_ui_text(
            "video_script_prompt",
            max_length=llm.MAX_SCRIPT_PROMPT_LENGTH,
        ),
        "custom_system_prompt": _saved_ui_text(
            "custom_system_prompt",
            llm.DEFAULT_SCRIPT_SYSTEM_PROMPT,
            llm.MAX_SCRIPT_SYSTEM_PROMPT_LENGTH,
        ),
        "match_materials_to_script": bool(
            config.app.get("match_materials_to_script", False)
        ),
        "custom_bgm_file_input": _saved_ui_text("custom_bgm_file"),
        "sonilo_bgm_prompt_input": _saved_ui_text(
            "sonilo_bgm_prompt",
            max_length=sonilo_service.MAX_PROMPT_LENGTH,
        ),
        "elevenlabs_music_prompt_input": _saved_ui_text(
            "elevenlabs_music_prompt",
            max_length=elevenlabs_music_service.MAX_PROMPT_LENGTH,
        ),
        "subtitle_enabled_checkbox": _saved_ui_bool("subtitle_enabled", True),
        "stroke_color_picker": _saved_ui_color("stroke_color", "#000000"),
        "stroke_width_slider": _saved_ui_number(
            "stroke_width", 1.5, 0.0, 10.0
        ),
        "loomloom_candidate_count": _saved_ui_number(
            "loomloom_candidate_count",
            3,
            1,
            loomloom.MAX_SCRIPT_CANDIDATES,
            int,
        ),
        "loomloom_script_duration_seconds": _saved_ui_number(
            "loomloom_script_duration_seconds", 60, 10, 600, int
        ),
        "ui_language": initial_ui_language,
        # 侧边导航默认落在“工作台”（以项目为中心的创作入口）。三级导航：
        # 工作台 / 素材库 / 设置；旧的 generate/overview/projects/tasks 等
        # 页面由 mpt_admin._normalize_page 归一到新的顶层区。
        "admin_page": "workspace",
        # 已落盘的本地素材允许用户只修改文案后继续复用。
        "local_video_materials": [],
        # 生成按钮回调先登记任务，使顶部入口能立即显示运行中数量。
        "active_generation_tasks": {},
        # 最近一次从当前页面提交的任务。生成改为后台执行后，页面 Fragment
        # 通过这个 ID 查询状态；刷新时不再依赖正在执行的旧页面脚本。
        "current_generation_task_id": "",
        # LoomLoom 询价与执行必须跨 Streamlit rerun 保留完全相同的输入和
        # clientRequestId，避免网络重试产生重复付费任务。
        "loomloom_script_batch": None,
        "loomloom_script_quote": None,
        "loomloom_script_input_signature": "",
        "loomloom_client_request_id": "",
        "loomloom_run_id": "",
        "loomloom_run_status": "",
        "loomloom_run_error": "",
        "loomloom_poll_failure_count": 0,
        "loomloom_poll_retry_after": 0.0,
        "loomloom_poll_paused": False,
        "loomloom_script_candidates": (),
        "loomloom_candidate_errors": (),
        "loomloom_selected_candidate": 0,
        "loomloom_video_batch": None,
        "loomloom_video_quote": None,
        "loomloom_video_input_signature": "",
        "loomloom_video_client_request_id": "",
        "loomloom_video_confirm_charge": False,
        "loomloom_video_quote_error_signature": "",
        "loomloom_video_quote_error": "",
        "loomloom_video_capability": None,
        "loomloom_video_capability_fingerprint": "",
        "loomloom_video_capability_load_attempt": "",
        "loomloom_video_capability_error": "",
        "loomloom_video_model_id": "",
        # 文案或完整配音刚生成时，在视频数量控件创建前消费这个摘要并自动
        # 填入推荐素材数；消费后即清空，避免覆盖用户后续手动调整。
        "loomloom_video_scene_autofill_digest": "",
        "wavespeed_confirm_charge": False,
        "volcengine_seedance_confirm_charge": False,
        "ofox_confirm_charge": False,
        "metaso_minimax_confirm_charge": False,
        "muapi_confirm_charge": False,
        # AI 视频按素材段计费，默认只生成一段，用户确认效果后再主动增加数量。
        "loomloom_video_scene_count": _saved_ui_number(
            "loomloom_video_scene_count",
            1,
            1,
            loomloom.MAX_VIDEO_SCENES,
            int,
        ),
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)



def stable_segmented_control(
    label, options, default_value, key, format_func=None, **kwargs
):
    """使用稳定业务值创建单选分段控件，避免语言切换后状态被展示文案覆盖。"""
    options = list(options)
    if not options:
        raise ValueError(f"segmented control options cannot be empty: {key}")

    if default_value not in options:
        default_value = options[0]

    widget_key = localized_widget_key(key)
    if st.session_state.get(widget_key) not in options:
        st.session_state[widget_key] = default_value

    return st.segmented_control(
        label,
        options=options,
        selection_mode="single",
        required=True,
        format_func=format_func or str,
        key=widget_key,
        **kwargs,
    )

def _estimate_voiceover_duration_range(
    text: str, voice_rate: float
) -> tuple[float, float] | None:
    """
    在本地估算完整配音时长，返回保守的上下界秒数。

    该估算只用于帮助用户在调用付费 TTS 前判断文案量级，不参与任务执行。
    中文、日文和韩文按字符速度估算，其它使用空格分词的语言按单词速度估算，
    再计入常见标点停顿。不同 Provider、音色和语气会造成实际偏差，因此界面
    必须展示区间而不是伪精确的单一结果。
    """
    normalized_text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized_text:
        return None

    script_chars = re.findall(
        r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]",
        normalized_text,
    )
    remaining_text = re.sub(
        r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]",
        " ",
        normalized_text,
    )
    words = re.findall(r"\b[\w]+(?:[-'’][\w]+)*\b", remaining_text, re.UNICODE)
    punctuation_count = len(re.findall(r"[,，.。!?！？;；:：]", normalized_text))

    # 4.2 字/秒和 2.6 词/秒接近日常解说语速；标点按 0.12 秒加入轻微停顿。
    # voice_rate 只作为估算修正项。部分生成式 TTS 不严格执行倍率，所以最终
    # 仍保留 ±15% 区间，避免让用户误以为该值等同于服务端真实结果。
    base_seconds = len(script_chars) / 4.2 + len(words) / 2.6 + punctuation_count * 0.12
    if base_seconds <= 0:
        return None

    normalized_rate = max(float(voice_rate or 1.0), 0.1)
    estimated_seconds = base_seconds / normalized_rate
    return (
        round(max(estimated_seconds * 0.85, 1.0), 1),
        round(max(estimated_seconds * 1.15, 1.0), 1),
    )