# webui/Main.py — MoneyPrinterTurbo WebUI 薄入口（Streamlit 入口点）。
# 原 Main.py (8569 行) 已拆分为:
#   webui/base.py     — i18n / 控件 / 常量 / 运行时配置 / 会话初始化
#   webui/tasks.py    — 任务管理 / 参数恢复 / 首次引导 / 人物选择 / 顶部栏
#   webui/scripts.py  — 脚本生成 / LoomLoom 后端
#   webui/video.py    — 视频设置 / 素材提供商
#   webui/audio.py    — 音频 / 音色预览 / 背景音乐
#   webui/subtitles.py— 字幕设置
#   webui/generation.py— 生成控制 / 传记向导
#
# 此文件只做: sys.path 修正、模块 import、st.set_page_config、
#             会话初始化、_render_application()、入口调用。

import os
import sys

root_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if root_dir in sys.path:
    sys.path.remove(root_dir)
sys.path.insert(0, root_dir)

from webui import base
from webui import tasks
from webui import audio
from webui import scripts
from webui import video
from webui import subtitles
from webui import settings
from webui import generation
from webui import mpt_admin
from webui import workspace

from app.models.schema import VideoParams

import streamlit as st

base._initialize_session_state()


def _render_application():
    """渲染侧导航、顶部栏、弹窗，并按选中页渲染工作区或管理面板。"""
    # 样式必须在脚本运行期注入(import 期注入会被 Streamlit 丢弃)。
    base.inject_styles()
    mpt_admin.render_admin_sidebar()
    # 侧栏已把旧页值归一到三级导航；此处再兜底一次，默认落在工作台。
    admin_page = mpt_admin._normalize_page(
        st.session_state.get("admin_page", "workspace")
    )
    tasks._render_top_bar()

    if st.session_state.get("settings_dialog_open", False):
        settings._render_settings_dialog()

    if admin_page == "workspace":
        # 以项目为中心的工作区：未打开项目→列表；已打开→分步编辑器。
        # 编辑过程只写项目 draft，不再调用 base._save_runtime_config()，
        # 从根本上避免跨项目串味（只有“设置”页写 config.toml）。
        tasks._render_first_run_onboarding()

        if settings._apply_pending_settings_preset():
            st.success(base.tr("Settings Preset Imported"))

        restore_applied = tasks._apply_pending_task_restore()
        restore_candidate_id = st.session_state.get("task_restore_candidate_id")
        if restore_candidate_id:
            tasks._render_task_restore_dialog(restore_candidate_id)
        restore_succeeded = st.session_state.pop("task_restore_succeeded", False)
        if restore_applied or restore_succeeded:
            st.success(base.tr("Task Configuration Loaded"))

        workspace.render_workspace_root()
        return

    mpt_admin.render_admin(admin_page)


_render_application()
