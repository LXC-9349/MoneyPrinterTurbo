"""WebUI 数据管理面板（侧边导航 + 管理页）。

侧边菜单（与主生成页互斥切换）：
  generate  生成视频（默认，即 Main.py 原有生成流程）
  tasks     任务管理：任务列表、状态筛选、播放/打开/复制/删除
  storage   存储管理：各子目录占用、视频缓存统计与清理
  library   资料库：只读语料库统计 + 人物检索
  persons   人物管理：新建/编辑/置顶/归档、分组管理

数据源（只改用户可控层，语料库只读）：
  storage/tasks/                     任务目录
  storage/{cache_videos, local_videos,
           cache_material_search, bgm,
           library}                  缓存/素材/语料
  storage/library/personal.sqlite    用户人物/分组（可写，经 person_store）

依赖只取轻服务；state 与 library 按需惰性导入，避免无谓拉起重依赖。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import webbrowser
import html
from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st
from loguru import logger

from app.models import const
from app.utils import utils
from app.services import cache_manager
from app.services import person_store

__all__ = ["render_admin_sidebar", "render_admin"]

# ---------------------------------------------------------------------------
# i18n：复用项目 load_locales（webui/i18n），翻译算法与 Main.py 的 tr() 一致：
#   当前语言 → 英文 → 原始 key
# 这里独立实现，避免与 Main.py 的循环依赖。
# ---------------------------------------------------------------------------
_I18N_DIR = os.path.join(utils.root_dir(), "webui", "i18n")
_LOCALES: dict[str, dict[str, Any]] = utils.load_locales(_I18N_DIR)

_PAGE_ICON = {
    "workspace": "🎬",
    "library": "📚",
    "settings": "⚙️",
    # 旧页（仅供向后兼容映射与标题回退）
    "overview": "📊",
    "generate": "🎬",
    "projects": "📁",
    "tasks": "📋",
    "storage": "🗂️",
    "persons": "👤",
}
_PAGE_LABEL_KEY = {
    "workspace": "ws_menu_workspace",
    "library": "ws_menu_library",
    "settings": "ws_menu_settings",
    "overview": "admin_menu_overview",
    "generate": "admin_menu_generate",
    "projects": "projects_menu",
    "tasks": "admin_menu_tasks",
    "storage": "admin_menu_storage",
    "persons": "admin_menu_persons",
}
# 三级导航：工作台 / 素材库 / 设置（替换原来的 7 项平铺 radio）。
_ADMIN_PAGES = (
    "workspace",
    "library",
    "settings",
)
# 向后兼容：旧代码/书签写入的旧页值统一映射到新的顶层区，避免功能失联。
_LEGACY_PAGE_MAP = {
    "generate": "workspace",
    "projects": "workspace",
    "tasks": "workspace",
    "overview": "workspace",
    "persons": "library",
    "storage": "settings",
}


def _normalize_page(page: str) -> str:
    """把任意旧/新页值归一为三级导航页；未知值回退工作台。"""
    page = str(page or "")
    if page in _ADMIN_PAGES:
        return page
    return _LEGACY_PAGE_MAP.get(page, "workspace")
# 失败阶段码（后端 _mark_task_failed 写入 failed_stage）→ 本地化展示名。
_STAGE_LABEL_KEYS = {
    "preflight": "admin_task_stage_preflight",
    "script": "admin_task_stage_script",
    "terms": "admin_task_stage_terms",
    "audio": "admin_task_stage_audio",
    "video": "admin_task_stage_video",
    "materials": "admin_task_stage_materials",
    "pipeline": "admin_task_stage_pipeline",
    "dequeue": "admin_task_stage_dequeue",
    "scheduling": "admin_task_stage_scheduling",
    "webui_worker": "admin_task_stage_webui_worker",
}
_STORAGE_DIRS = [
    ("tasks", "admin_storage_dir_tasks"),
    ("cache_videos", "admin_storage_dir_cache_videos"),
    ("local_videos", "admin_storage_dir_local_videos"),
    ("cache_material_search", "admin_storage_dir_cache_material_search"),
    ("bgm", "admin_storage_dir_bgm"),
    ("library", "admin_storage_dir_library"),
]
_SOURCE_LABEL_KEY = {
    "custom": "admin_persons_source_custom",
    "library": "admin_persons_source_library",
}


def t(key: str) -> str:
    """翻译：当前语言 → 英文 → 原始 key。"""
    lang = st.session_state.get("ui_language") or ""
    loc = (_LOCALES.get(lang) or {}).get("Translation") or {}
    if isinstance(loc, dict):
        v = loc.get(key)
        if v is not None:
            return v
    en = (_LOCALES.get("en") or {}).get("Translation") or {}
    if isinstance(en, dict):
        return en.get(key, key)
    return key


def _format_size_b(size_bytes: float) -> str:
    """按 1024 进制把字节格式化为人类可读大小。

    原实现中 ``if unit == "B": return`` 会立刻返回原始字节数,导致任何
    >=1024 字节的量都显示成 ``XXXXX B``,无法换算 KB/MB/GB。这里改为
    仅在小于 1024 时返回当前单位,否则除以 1024 进入下一个单位。
    """
    if size_bytes is None:
        return "-"
    size_bytes = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size_bytes < 1024.0:
            return f"{int(size_bytes)} B" if unit == "B" else f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"


def _human_mtime(mtime: float) -> str:
    if not mtime:
        return "-"
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")


def _is_headless() -> bool:
    """是否无可用桌面来打开本地文件。

    WSL 下 DISPLAY/WAYLAND_DISPLAY 常由 X11 转发设置(如 localhost:10.0),
    但容器里没有浏览器/文件管理器:此时调用 xdg-open 只会静默失败,用户点击
    "播放/打开"毫无反应。因此把 WSL 视为无桌面,改用页面内预览回退。
    """
    import sys

    if sys.platform == "darwin" or sys.platform.startswith("win"):
        return False
    # WSL:即使有 DISPLAY,也没有可直接打开文件的桌面程序。
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _open_path(path: str | Path | None) -> None:
    """打开本地文件（桌面部署用系统播放器/资源管理器；无桌面则忽略）。"""
    if not path:
        return
    try:
        p = Path(str(path))
        if p.exists():
            webbrowser.open(p.as_uri())
    except Exception:  # pragma: no cover
        logger.debug("failed to open path: %s", path)


def _open_dir(path: str | Path | None) -> None:
    if not path:
        return
    try:
        p = Path(str(path))
        if p.exists():
            webbrowser.open(p.as_uri())
    except Exception:  # pragma: no cover
        logger.debug("failed to open directory: %s", path)


def _inject_clipboard(text: str) -> None:
    """把文本写入浏览器剪贴板(前端 JS 一次性注入 + 调用)。"""
    payload = json.dumps(text, ensure_ascii=False)
    st.markdown(
        f"<script>"
        f"(()=>{{const txt={payload};"
        "if(navigator.clipboard){{navigator.clipboard.writeText(txt)};"
        "else{{var t=document.createElement('textarea');t.value=txt;"
        "document.body.appendChild(t);t.select();"
        "document.execCommand('copy');t.remove();}}}})();"
        "</script>",
        unsafe_allow_html=True,
    )


def render_admin_video_preview() -> None:
    """管理页内的成片播放回退(无桌面环境)。

    点击任务行的"播放"会把文件写入 session_state["task_preview_video_file"]。
    生成页有自己的消费点(tasks._render_task_video_preview),但管理页没有,
    于是在 WSL/无桌面下点"播放"毫无反应。这里在管理页末尾渲染页面内播放器。
    """
    preview_file = st.session_state.get("task_preview_video_file")
    if not preview_file:
        return
    tasks_root = os.path.abspath(utils.task_dir())
    if not (
        str(preview_file).startswith(tasks_root + os.sep)
        and os.path.isfile(str(preview_file))
    ):
        st.session_state.pop("task_preview_video_file", None)
        return

    st.divider()
    cols = st.columns([5, 1], vertical_alignment="center")
    task_name = os.path.basename(os.path.dirname(str(preview_file)))
    cols[0].caption(f"{os.path.basename(str(preview_file))} · {task_name}")
    if cols[1].button("✕", key="close_admin_video_preview", width="stretch"):
        st.session_state.pop("task_preview_video_file", None)
        st.rerun()
        return
    st.video(str(preview_file))


# ---------------------------------------------------------------------------
# 导航侧边栏
# ---------------------------------------------------------------------------
def render_admin_sidebar() -> None:
    """渲染三级导航侧栏（工作台/素材库/设置）。用户选择写入 admin_page。"""
    with st.sidebar:
        st.markdown(
            f"<div class='mpt-navbrand'>{_PAGE_ICON['workspace']} MoneyPrinterTurbo</div>",
            unsafe_allow_html=True,
        )

        # 归一 admin_page：消费中转键 admin_page_back（旧值经映射），并将在
        # 三级 radio 实例化前把旧页值归一到新页，否则 radio 因当前值不在
        # options 内会报错。必须在 radio 之前改绑定键。
        back = st.session_state.get("admin_page_back")
        if back:
            del st.session_state["admin_page_back"]
            st.session_state["admin_page"] = _normalize_page(back)
        current = _normalize_page(st.session_state.get("admin_page"))
        if st.session_state.get("admin_page") != current:
            st.session_state["admin_page"] = current

        # 展示文案必须在 radio 实例化时快照为常量映射,不能在 format_func 里实时
        # 读取 st.session_state:Streamlit 序列化 widget 状态时(脚本运行之外,例如
        # AppTest 的 get_widget_states)session_state 为空,实时 t() 会回退英文,与
        # 渲染期写入 proto 的中文选项不一致,options.index 随即抛 ValueError。
        label_map = {
            value: f"{_PAGE_ICON.get(value, '')} {t(_PAGE_LABEL_KEY.get(value, value))}"
            for value in _ADMIN_PAGES
        }
        st.sidebar.radio(
            "menu",
            _ADMIN_PAGES,
            key="admin_page",
            format_func=lambda value: label_map.get(value, value),
            label_visibility="collapsed",
        )

        # 侧栏底部快捷：新建项目 + 最近项目入口 + 设置弹窗。
        # 导航改用 on_click 回调：回调在下一轮脚本运行、上方 admin_page radio
        # 实例化之前执行，因此可安全写 st.session_state["admin_page"]；若像以前
        # 那样在按钮分支里内联赋值，会报 "cannot be modified after the widget
        # with key admin_page is instantiated"。
        def _sidebar_goto(pid: str, new: bool) -> None:
            from webui import workspace  # noqa: WPS437

            if new:
                pid = workspace.create_project("biography")
                workspace.open_workspace(pid, workspace.STEP_CONTENT)
            else:
                workspace.open_workspace(pid)
            st.session_state["admin_page"] = "workspace"

        st.divider()
        st.button(
            t("ws_new_project"),
            key="ws_sidebar_new",
            icon=":material/add:",
            width="stretch",
            type="primary",
            on_click=_sidebar_goto,
            args=("", True),
        )

        recent = _recent_project_ids()
        if recent:
            st.caption(t("ws_recent_projects"))
            for pid, title in recent:
                st.button(
                    f"• {title}",
                    key=f"ws_sidebar_recent_{pid}",
                    width="stretch",
                    on_click=_sidebar_goto,
                    args=(pid, False),
                )

        st.divider()
        if st.button(t("admin_sidebar_settings"), key="admin_sidebar_settings", icon=":material/settings:"):
            st.session_state["settings_dialog_open"] = True


def _recent_project_ids(limit: int = 5) -> list[tuple[str, str]]:
    """最近编辑的项目（id, 标题），供侧栏快捷入口。"""
    try:
        from webui import projects  # noqa: WPS437

        entries = sorted(
            projects._load_projects(),
            key=lambda e: e.get("updated_at") or e.get("created_at") or 0,
            reverse=True,
        )
    except Exception:  # pragma: no cover - 读取失败不影响导航
        return []
    out: list[tuple[str, str]] = []
    for e in entries:
        pid = e.get("id")
        if not pid:
            continue
        title = (e.get("title") or e.get("subject") or pid)[:22]
        out.append((pid, title))
        if len(out) >= limit:
            break
    return out


def render_admin(page: str) -> None:
    """按页面名渲染对应管理面板（新三级导航：library / settings）。

    workspace 不在此处理（由 Main.py 路由到 workspace.render_workspace_root）。
    旧页值经 _normalize_page 归一到新页，保证向后兼容。
    """
    page = _normalize_page(page)
    with st.container(key="admin_page_banner"):
        st.markdown(f"## {_PAGE_ICON.get(page, '')} {t(_PAGE_LABEL_KEY.get(page, page))}")
    if page == "library":
        render_library_merged_page()
    elif page == "settings":
        render_settings_page()
    elif page == "workspace":
        from webui import workspace  # noqa: WPS437

        workspace.render_workspace_root()
    else:
        st.info(t("admin_page_missing"))

    # 任务行"播放"在无桌面时写入预览文件;管理页需在此消费,否则点击无反应。
    render_admin_video_preview()


def render_library_merged_page() -> None:
    """素材库：文档/检索 与 人物管理合并为子标签。"""
    tab_docs, tab_persons = st.tabs(
        [t("ws_lib_tab_docs"), t("ws_lib_tab_persons")]
    )
    with tab_docs:
        render_library_page()
    with tab_persons:
        render_persons_page()


def render_settings_page() -> None:
    """设置：全局配置入口 + 存储/缓存管理。"""
    bar = st.container()
    if bar.button(
        t("ws_settings_open"),
        key="ws_settings_open_dialog",
        icon=":material/tune:",
        type="primary",
    ):
        st.session_state["settings_dialog_open"] = True
    render_storage_page()


# ---------------------------------------------------------------------------
# 任务管理页
# ---------------------------------------------------------------------------
def _load_task_script(task_path: str) -> dict[str, Any]:
    script_file = os.path.join(task_path, "script.json")
    if not os.path.isfile(script_file):
        return {}
    try:
        with open(script_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:  # pragma: no cover
        logger.debug("failed to read task script %s: %s", script_file, exc)
        return {}


def _find_final_video(task_path: str) -> str:
    """返回序号最小的成片（与 Main.py 的 _FINAL_VIDEO_PATTERN 一致）。"""
    try:
        files = os.listdir(task_path)
    except OSError:
        return ""
    candidates = []
    for name in files:
        if re.fullmatch(r"final-\d+\.(mp4|mov|mkv|webm)", name, re.IGNORECASE):
            candidates.append((int(re.search(r"final-(\d+)", name).group(1)), name))
    if not candidates:
        return ""
    idx, file_name = min(candidates)
    return os.path.join(task_path, file_name)


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, _files in os.walk(path):
        for name in _files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _task_rows() -> list[dict[str, Any]]:
    """聚合历史目录 + 运行时状态，产出统一任务列表（最新优先，最多 200）。"""
    tasks_root = utils.task_dir()
    if not os.path.isdir(tasks_root):
        return []

    runtime_map: dict[str, dict[str, Any]] = {}
    try:
        from app.services import state as sm  # 按需导入

        runtime_tasks, _total = sm.state.get_all_tasks(1, 200)
        for task in runtime_tasks or []:
            tid = str(task.get("task_id", ""))
            if tid:
                runtime_map[tid] = task
    except Exception:
        runtime_map = {}

    try:
        entries = []
        with os.scandir(tasks_root) as it:
            for entry in it:
                if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    entries.append((entry.stat(follow_symlinks=False).st_mtime, entry.path))
                except OSError:
                    pass
    except OSError:
        return []

    entries.sort(key=lambda item: item[0], reverse=True)
    rows: list[dict[str, Any]] = []
    for mtime, task_path in entries[:200]:
        task_id = os.path.basename(task_path)
        script = _load_task_script(task_path)
        params = script.get("params", {}) or {}
        video = _find_final_video(task_path)
        subject = (
            str(params.get("video_subject") or "").strip()
            or (script.get("script") or "").strip()[:60]
            or task_id
        )
        state_val = runtime_map.get(task_id, {}).get("state")
        if state_val is None:
            state_val = const.TASK_STATE_COMPLETE if video else const.TASK_STATE_FAILED
        rt = runtime_map.get(task_id, {})
        rows.append(
            {
                "task_id": task_id,
                "subject": subject or task_id,
                "state": state_val,
                "progress": int(rt.get("progress") or 0),
                "video_file": video,
                "dir_size": _dir_size(task_path),
                "mtime": mtime,
                "task_path": task_path,
                "script": script,
                # 失败诊断：运行时状态里保存的错误与失败阶段（enable_redis 时跨重启保留）。
                "error": str(rt.get("error") or "").strip(),
                "failed_stage": str(rt.get("failed_stage") or "").strip(),
                # 与主生成页 _queue_task_restore 的恢复判定一致：存在 script.json 才可重跑。
                "has_restore": os.path.isfile(os.path.join(task_path, "script.json")),
            }
        )
    return rows


def _status_label(task: dict[str, Any]) -> str:
    state = task.get("state")
    if state == const.TASK_STATE_PROCESSING:
        return t("admin_task_processing")
    if state == const.TASK_STATE_FAILED:
        return t("admin_task_failed")
    if state == const.TASK_STATE_COMPLETE or task.get("video_file"):
        return t("admin_task_complete")
    return t("admin_task_history")


def _delete_task(task: dict[str, Any]) -> bool:
    task_path = task.get("task_path", "")
    task_id = task.get("task_id", "")
    # 进行中任务拒绝删除（与 Main.py 一致）。
    if task.get("state") == const.TASK_STATE_PROCESSING:
        return False
    current_state = None
    try:
        from app.services import state as sm

        current_state = (sm.state.get_task(task_id) or {}).get("state")
    except Exception:
        pass
    if current_state == const.TASK_STATE_PROCESSING:
        return False
    root = os.path.abspath(utils.task_dir())
    normalized = os.path.abspath(str(task_path))
    if not normalized.startswith(root + os.sep):
        return False
    if not os.path.isdir(normalized):
        return False
    try:
        from app.services import state as sm

        sm.state.delete_task(task_id)
        shutil.rmtree(normalized, ignore_errors=True)
        return True
    except Exception as exc:  # pragma: no cover
        logger.warning("failed to delete task %s: %s", task_id, exc)
        return False


def _task_is_failed(task: dict[str, Any]) -> bool:
    return task.get("state") == const.TASK_STATE_FAILED


def _task_is_done(task: dict[str, Any]) -> bool:
    return task.get("state") == const.TASK_STATE_COMPLETE or bool(task.get("video_file"))


def _stage_display(task: dict[str, Any]) -> str:
    stage = (task.get("failed_stage") or "").strip()
    if not stage:
        return ""
    return t(_STAGE_LABEL_KEYS.get(stage, "")) or stage


def _status_badge(task: dict[str, Any]) -> tuple[str, str]:
    """返回(状态文案, 徽章颜色类: ok/warn/err/mut)。"""
    state = task.get("state")
    if state == const.TASK_STATE_PROCESSING:
        return t("admin_task_processing"), "warn"
    if state == const.TASK_STATE_FAILED:
        return t("admin_task_failed"), "err"
    if state == const.TASK_STATE_COMPLETE or task.get("video_file"):
        return t("admin_task_complete"), "ok"
    return t("admin_task_history"), "mut"


def _render_task_card(index: int, task: dict[str, Any]) -> None:
    """单条任务以卡片展示: 状态徽章 + 主题/进度 + 快捷操作按钮。"""
    safe_id = "".join(ch if ch.isalnum() else "_" for ch in task["task_id"])[:40]
    processing = task.get("state") == const.TASK_STATE_PROCESSING
    failed = _task_is_failed(task)
    done = _task_is_done(task)
    has_restore = bool(task.get("has_restore"))
    label, color = _status_badge(task)
    progress = int(task.get("progress") or 0)
    subject = (task.get("subject") or task["task_id"])[:80]
    badge_cls = f"mpt-task-badge mpt-task-badge-{color}"

    with st.container(key=f"admin_task_card_{index}_{safe_id}", border=True):
        top = st.columns([1.1, 4.9, 1.0], vertical_alignment="center", gap="small")
        top[0].markdown(
            f'<span class="{badge_cls}">{label}</span>', unsafe_allow_html=True
        )
        top[1].write(subject)
        top[2].write(f"{progress}%")
        meta = st.columns([5.0, 1.0], vertical_alignment="center", gap="small")
        if failed:
            stage = _stage_display(task)
            meta[0].caption(t("admin_task_failure_stage") + ": " + (stage or t("admin_task_failure_unknown")))
        else:
            meta[0].caption(_human_mtime(task.get("mtime", 0)))
        meta[1].write("")

        ac = st.columns(5, vertical_alignment="center", gap="small")
        with ac[0]:
            if st.button(
                t("admin_task_play"),
                key=f"admin_play_{safe_id}",
                icon=":material/play_arrow:",
                width="stretch",
                disabled=not task.get("video_file"),
            ):
                if _is_headless() and task.get("video_file"):
                    st.session_state["task_preview_video_file"] = task["video_file"]
                    st.rerun()
                else:
                    _open_path(task.get("video_file"))
        with ac[1]:
            if st.button(
                t("admin_task_open"),
                key=f"admin_open_{safe_id}",
                icon=":material/folder_open:",
                width="stretch",
            ):
                _open_dir(task.get("task_path"))
        with ac[2]:
            if st.button(
                t("admin_task_copy"),
                key=f"admin_copy_{safe_id}",
                icon=":material/content_copy:",
                width="stretch",
            ):
                script = task.get("script") or {}
                text = (script.get("script") or "").strip()
                if text:
                    _inject_clipboard(text)
                else:
                    st.error(t("admin_task_copy_none"))
        retry_enabled = (not processing) and has_restore and (failed or done)
        retry_key = "admin_task_retry" if failed else "admin_task_regenerate"
        with ac[3]:
            if failed or done:
                if st.button(
                    t(retry_key),
                    key=f"admin_retry_{safe_id}",
                    icon=":material/replay:",
                    width="stretch",
                    type="primary" if failed else "secondary",
                    disabled=not retry_enabled,
                ):
                    st.session_state["task_restore_candidate_id"] = task["task_id"]
                    st.session_state["task_manager_popover_nonce"] = (
                        st.session_state.get("task_manager_popover_nonce", 0) + 1
                    )
                    st.session_state["admin_page_back"] = "generate"
                    st.rerun(scope="app")
        with ac[4]:
            if st.button(
                t("admin_task_delete"),
                key=f"admin_del_{safe_id}",
                icon=":material/delete:",
                width="stretch",
                type="secondary",
                disabled=processing,
            ):
                if _delete_task(task):
                    st.toast(t("admin_task_deleted"))
                    st.rerun()
                else:
                    st.error(t("admin_task_delete_failed"))
        script = task.get("script") or {}
        with st.container(key=f"admin_script_expander_{safe_id}"):
            if st.expander(t("admin_task_view_script"), key=f"admin_script_exp_{safe_id}"):
                st.code((script.get("script") or "")[:6000], language="text")
        if failed:
            _render_task_diagnosis(task, safe_id)




def _render_task_diagnosis(task: dict[str, Any], safe_id: str) -> None:
    error = (task.get("error") or "").strip()
    stage = _stage_display(task)
    progress = int(task.get("progress") or 0)
    with st.container(key=f"admin_task_diagnosis_{safe_id}", border=False):
        stage_cell, reason_cell = st.columns(
            [1.4, 8.6], vertical_alignment="center", gap="small"
        )
        if stage:
            stage_cell.markdown(f"**{t('admin_task_failure_stage')}**：{stage}")
        else:
            stage_cell.markdown("**--**")
        stage_cell.markdown(f"**{t('admin_task_failure_progress')}**：{progress}%")
        reason_cell.caption(t("admin_task_failure_reason"))
        if error:
            reason_cell.caption(error[:120] + ("…" if len(error) > 120 else ""))
        else:
            reason_cell.caption(t("admin_task_failure_unknown"))
        with st.container(key=f"admin_task_failure_detail_{safe_id}"):
            if st.expander(
                t("admin_task_failure_details"), key=f"admin_task_failure_exp_{safe_id}"
            ):
                if error:
                    st.code(error, language="text")
                else:
                    st.caption(t("admin_task_failure_no_detail"))


def render_tasks_page() -> None:
    st.markdown(t("admin_tasks_hint"))
    search, status_filter, _ = st.columns([2.2, 1.2, 0.8], vertical_alignment="center")
    keyword = search.text_input(
        t("admin_tasks_search"),
        placeholder=t("admin_tasks_search_ph"),
        key="admin_tasks_search",
    )
    status = status_filter.selectbox(
        t("admin_tasks_status"),
        options=["all", "processing", "complete", "failed"],
        index=0,  # Streamlit 1.59.1 的 selectbox 用 index 指定默认项（"all" 为第一项）
        key="admin_tasks_status",
        format_func={
            "all": t("admin_tasks_all"),
            "processing": t("admin_tasks_processing"),
            "complete": t("admin_tasks_complete"),
            "failed": t("admin_tasks_failed"),
        }.__getitem__,
    )

    all_tasks = _task_rows()
    # 健康度指标基于全部任务（不受筛选/搜索影响），作为页面顶部的稳定总览。
    total_tasks = len(all_tasks)
    complete_tasks = sum(1 for tk in all_tasks if _task_is_done(tk))
    processing_tasks = sum(
        1 for tk in all_tasks if tk.get("state") == const.TASK_STATE_PROCESSING
    )
    failed_tasks = sum(
        1 for tk in all_tasks if tk.get("state") == const.TASK_STATE_FAILED
    )

    m1, m2, m3, m4 = st.columns(4, vertical_alignment="center", gap="small")
    m1.metric(t("admin_tasks_metric_total"), total_tasks)
    m2.metric(t("admin_tasks_metric_complete"), complete_tasks)
    m3.metric(t("admin_tasks_metric_processing"), processing_tasks)
    m4.metric(t("admin_tasks_metric_failed"), failed_tasks)

    tasks = all_tasks
    if status != "all":
        def _key(task: dict[str, Any]) -> str:
            state = task.get("state")
            if state == const.TASK_STATE_PROCESSING:
                return "processing"
            if state == const.TASK_STATE_FAILED:
                return "failed"
            if state == const.TASK_STATE_COMPLETE or task.get("video_file"):
                return "complete"
            return "history"

        tasks = [tk for tk in tasks if _key(tk) == status]
    kw = keyword.strip().lower()
    if kw:
        tasks = [tk for tk in tasks if kw in (tk.get("subject") or "").lower()]

    if not tasks:
        st.info(t("admin_tasks_empty"))
        return

    for i, task in enumerate(tasks):
        _render_task_card(i, task)

    if st.button(t("admin_tasks_open_root"), key="admin_tasks_open_root", icon=":material/expand_more:"):
        _open_dir(utils.task_dir())


# ---------------------------------------------------------------------------
# 存储管理页
# ---------------------------------------------------------------------------
def _storage_usage() -> list[dict[str, Any]]:
    root = utils.storage_dir()
    rows = []
    for name, key in _STORAGE_DIRS:
        d = os.path.join(root, name)
        count = 0
        size = 0
        exists = os.path.isdir(d)
        if exists:
            for rd, _dirs, _files in os.walk(d):
                for fn in _files:
                    try:
                        size += os.path.getsize(os.path.join(rd, fn))
                        count += 1
                    except OSError:
                        pass
        rows.append({"name": name, "key": key, "count": count, "size": size, "exists": exists})
    return rows


def render_storage_page() -> None:
    rows = _storage_usage()
    total = sum(r["size"] for r in rows)
    st.metric(t("admin_storage_total"), _format_size_b(total))

    st.divider()
    with st.container(key="admin_storage_cards"):
        for row in rows:
            label = t(row["key"])
            missing = "" if row["exists"] else f" {t('admin_storage_dir_missing')}"
            dir_path = os.path.join(utils.storage_dir(), row["name"])
            with st.container(key=f"admin_storage_card_{row['name']}", border=True):
                c1, c2, c3, c4 = st.columns([3.4, 1.6, 1.6, 2.0], vertical_alignment="center", gap="small")
                c1.write(f"**{label}**{missing}")
                c2.caption(f"{t('admin_storage_file_count')}: {row['count']}")
                c3.caption(_format_size_b(row["size"]))
                with c4:
                    if st.button(
                        t("admin_storage_dir_open"),
                        key=f"admin_storage_open_{row['name']}",
                        icon=":material/folder_open:",
                        width="stretch",
                        disabled=not row["exists"],
                    ):
                        _open_dir(dir_path)
    _render_video_cache_block()


def _render_video_cache_block() -> None:
    st.caption(t("admin_video_cache_dir"))
    try:
        st.code(cache_manager.video_cache_dir(), language="text")
    except Exception:
        pass

    try:
        stats = cache_manager.get_video_cache_stats()
    except Exception as exc:  # pragma: no cover
        st.error(str(exc))
        return

    m1, m2, m3 = st.columns(3, vertical_alignment="center")
    m1.metric(t("admin_video_cache_count"), stats.file_count)
    m2.metric(t("admin_video_cache_size"), _format_size_b(stats.total_size))
    m3.metric(t("admin_video_cache_oldest"), _human_mtime(stats.oldest_mtime) if stats.oldest_mtime else "-")

    if st.button(t("admin_video_cache_refresh"), key="admin_video_cache_refresh", icon=":material/refresh:", width="stretch"):
        cache_manager.get_video_cache_stats.cache_clear()
        st.rerun()

    options = (None, 7, 30, 90)
    label_map = {
        None: t("admin_video_cache_all"),
        7: t("admin_video_cache_7"),
        30: t("admin_video_cache_30"),
        90: t("admin_video_cache_90"),
    }
    max_age = st.selectbox(
        t("admin_video_cache_range"),
        options=options,
        format_func=label_map.__getitem__,
        key="admin_video_cache_range",
    )
    try:
        preview = cache_manager.get_video_cache_stats(max_age_days=max_age)
    except Exception:  # pragma: no cover
        preview = cache_manager.VideoCacheCleanupResult(failed_count=1)  # type: ignore[attr-defined]
        st.rerun()
    st.info(
        t("admin_video_cache_preview").format(
            count=preview.file_count, size=_format_size_b(preview.total_size)
        )
    )
    with st.container(key="admin_video_cache_actions"):
        if st.checkbox(t("admin_video_cache_confirm"), key="admin_video_cache_confirm"):
            if st.button(
                t("admin_video_cache_clean"),
                type="primary",
                key="admin_video_cache_clean",
                icon=":material/delete_sweep:",
                width="stretch",
                disabled=preview.file_count == 0,
            ):
                try:
                    result = cache_manager.clean_video_cache(max_age_days=max_age)
                except Exception as exc:  # pragma: no cover
                    st.error(str(exc))
                    return
                if result.failed_count:
                    st.warning(t("admin_video_cache_fail").format(fail=result.failed_count))
                st.success(
                    t("admin_video_cache_done").format(
                        count=result.deleted_count, size=_format_size_b(result.deleted_size)
                    )
                )
                cache_manager.get_video_cache_stats.cache_clear()
                st.rerun()


# ---------------------------------------------------------------------------
# 资料库页（只读语料库统计 + 人物检索）
# ---------------------------------------------------------------------------
def _library_counts() -> dict[str, Any]:
    try:
        from app.services import library

        if not library.enabled():
            return {}
        db = library.db_path()
        if db is None:
            return {}
        import sqlite3

        conn = sqlite3.connect(str(db), timeout=30)
        counts: dict[str, Any] = {}
        for name in ("documents", "books", "chapters", "persons"):
            try:
                counts[name] = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            except sqlite3.Error:
                counts[name] = None
        conn.close()
        return counts
    except Exception as exc:  # pragma: no cover
        logger.debug("library stats failed: %s", exc)
        return {}


def _lookup_person(name: str):
    try:
        from app.services import library

        if not library.enabled():
            return None
        return library.get_person_profile(name, max_docs=8)
    except Exception:
        return None


def _render_person_profile(profile: dict[str, Any], name: str) -> None:
    with st.container(border=True, key="admin_library_profile"):
        st.write(f"### {name}")
        dynasty = profile.get("dynasty") or profile.get("era_en") or ""
        docs = profile.get("doc_count")
        if dynasty:
            st.caption(t("admin_library_profile_dynasty").format(dynasty=dynasty))
        if docs is not None:
            st.caption(t("admin_library_profile_docs").format(docs=int(docs)))
        facts = profile.get("key_facts") or profile.get("facts") or []
        if facts:
            st.caption(t("admin_library_profile_facts"))
            for f in list(facts)[:12]:
                st.markdown(f"- {f}")


def render_library_page() -> None:
    counts = _library_counts()
    if not counts:
        st.warning(t("admin_library_unavailable"))
        return

    c1, c2, c3, c4 = st.columns(4, vertical_alignment="center")
    for col, key in zip((c1, c2, c3, c4), ("documents", "books", "chapters", "persons")):
        col.metric(
            t({
                "documents": "admin_library_docs",
                "books": "admin_library_books",
                "chapters": "admin_library_chapters",
                "persons": "admin_library_persons",
            }[key]),
            counts.get(key, "-"),
        )

    st.divider()
    name = st.text_input(
        t("admin_library_search"),
        placeholder=t("admin_library_search_ph"),
        key="admin_library_search",
    )
    with st.container(key="admin_library_search_action"):
        if st.button(t("admin_library_search_btn"), key="admin_library_search_btn", icon=":material/search:", width="stretch"):
            if not name.strip():
                st.error(t("admin_library_search_required"))
                return
            profile = _lookup_person(name.strip())
            if profile is None:
                st.info(t("admin_library_none").format(name=name.strip()))
            else:
                _render_person_profile(profile, name.strip())


# ---------------------------------------------------------------------------
# 人物管理页（可写：person_store）
# ---------------------------------------------------------------------------
def _person_groups(name: str) -> list[str]:
    try:
        return person_store.groups_of(name)
    except Exception:
        return []


def render_persons_page() -> None:
    render_persons_new_section()

    search, dynasty_box, cat_box = st.columns([2.6, 1.4, 1.2], vertical_alignment="center")
    keyword = search.text_input(
        t("admin_persons_search"),
        placeholder=t("admin_persons_search_ph"),
        key="admin_persons_search",
    )
    dynasty = dynasty_box.selectbox(
        t("admin_persons_dynasty"),
        options=[""] + [str(x) for x in _dynasty_options()],
        key="admin_persons_dynasty",
    )
    cat = cat_box.selectbox(
        t("admin_persons_category"),
        options=[""] + [str(x) for x in _category_options()],
        key="admin_persons_category",
    )
    include_hidden = st.checkbox(t("admin_persons_show_hidden"))

    persons = person_store.list_persons(
        keyword=keyword.strip(),
        dynasty=dynasty or "",
        category=cat or "",
        include_hidden=include_hidden,
    )
    st.write(t("admin_persons_total").format(count=len(persons)))

    if st.session_state.get("admin_person_editor_open"):
        _render_person_editor()

    st.divider()

    # 分组管理
    with st.container(border=False, key="admin_persons_groups_box"):
        st.header(t("admin_persons_groups"))
        groups = _group_names()
        g_left, g_right = st.columns(2, vertical_alignment="center")
        with g_left:
            st.write(t("admin_persons_groups_current"))
            st.caption("、".join(groups[:30]) if groups else "-")
        with g_right:
            gname = st.text_input(t("admin_persons_new_group"), key="admin_persons_new_group")
            if st.button(t("admin_persons_create_group"), key="admin_persons_create_group", width="stretch", disabled=not gname.strip()):
                ok = person_store.create_group(gname.strip())
                if ok:
                    st.toast(t("admin_persons_group_created"))
                    st.rerun()
                else:
                    st.error(t("admin_persons_group_create_failed"))
            if st.button(t("admin_persons_delete_group"), key="admin_persons_delete_group", width="stretch", disabled=not groups):
                delete_g = st.selectbox(
                    t("admin_persons_delete_group_of"),
                    options=groups,
                    key="admin_persons_delete_group_of",
                )
                if person_store.delete_group(delete_g):
                    st.toast(t("admin_persons_group_deleted"))
                    st.rerun()

    st.divider()

    for i, p in enumerate(persons[:200]):
        name = p.get("person", "")
        safe = "".join(ch if ch.isalnum() else "_" for ch in name)[:40]
        src = t(_SOURCE_LABEL_KEY.get(p.get("source", "library"), "admin_persons_source_library"))
        docs = p.get("doc_count") or 0
        pinned = bool(p.get("pinned"))
        with st.container(key=f"admin_person_card_{i}_{safe}", border=True):
            # 操作区（编辑/置顶/删除）是三个带图标+中文标签的 stretch 按钮，窄列下
            # 容易裁切末字，故给它更大的列宽；中间的朝代/类别/文章数只是短值，收窄。
            top = st.columns([2.6, 1.0, 1.0, 0.6, 3.4], vertical_alignment="center", gap="small")
            top[0].markdown(
                "**" + html.escape(name) + "** "
                f"<span class='mpt-badge-mut'>{html.escape(src)}</span>",
                unsafe_allow_html=True,
            )
            top[1].write(p.get("dynasty") or "-")
            top[2].write(p.get("category") or "-")
            top[3].write(f"{docs}")
            ac = top[4].columns(3, vertical_alignment="center", gap="small")
            with ac[0]:
                if st.button(t("admin_persons_edit"), key=f"admin_person_edit_{safe}", width="stretch"):
                    st.session_state["admin_person_editor_name"] = name
                    st.session_state["admin_person_editor_open"] = True
                    st.rerun()
            with ac[1]:
                if st.button(
                    t("admin_persons_unpin") if pinned else t("admin_persons_pin"),
                    key=f"admin_person_pin_{safe}",
                    icon=":material/star:",
                    width="stretch",
                ):
                    person_store.update_person_meta(name, pinned=not pinned)
                    st.toast(t("admin_persons_pin_toggled"))
                    st.rerun()
            with ac[2]:
                if st.button(t("admin_persons_delete"), key=f"admin_person_del_{safe}", icon=":material/delete:", type="secondary", width="stretch"):
                    outcome = person_store.delete_person(name)
                    if outcome == "deleted":
                        st.toast(t("admin_persons_deleted"))
                    elif outcome == "archived":
                        st.toast(t("admin_persons_archived"))
                    else:
                        st.error(t("admin_persons_delete_failed"))
                    st.rerun()
        with st.container(key=f"admin_person_groups_{safe}"):
            with st.expander(t("admin_persons_groups_for"), key=f"admin_person_group_exp_{safe}", expanded=False):
                member_groups = _person_groups(name)
                all_groups = _group_names()
                selected_groups = st.multiselect(
                    t("admin_persons_selected_groups"),
                    options=all_groups,
                    default=member_groups if all_groups else [],
                    key=f"admin_person_group_select_{safe}",
                    label_visibility="collapsed",
                )
                if st.button(t("admin_persons_apply_groups"), key=f"admin_person_groups_apply_{safe}", width="stretch", disabled=not all_groups):
                    person_store.set_person_groups(name, list(selected_groups))
                    st.toast(t("admin_persons_groups_saved"))
                    st.rerun()

    if persons and len(persons) > 200:
        st.info(t("admin_persons_more").format(total=len(persons)))


@st.dialog(t("admin_persons_edit_title"))
def _render_person_editor() -> None:
    name = st.session_state.get("admin_person_editor_name") or ""
    if not name:
        st.session_state.pop("admin_person_editor_open", None)
        st.rerun()
        return
    safe = "".join(ch if ch.isalnum() else "_" for ch in name)[:40]
    meta = person_store.person_meta(name) or {}
    raw_facts = meta.get("facts_json")
    existing_facts: list[str] = []
    if isinstance(raw_facts, str):
        try:
            loaded = json.loads(raw_facts)
            if isinstance(loaded, list):
                existing_facts = [str(x) for x in loaded]
        except Exception:
            existing_facts = []
    elif isinstance(raw_facts, list):
        existing_facts = [str(x) for x in raw_facts]

    dynasty = st.text_input(
        t("admin_persons_dynasty"),
        value=meta.get("dynasty") or "",
        key=f"admin_edit_dynasty_{safe}",
    )
    category = st.text_input(
        t("admin_persons_category"),
        value=meta.get("category") or "",
        key=f"admin_edit_category_{safe}",
    )
    note = st.text_area(
        t("admin_persons_note"),
        value=meta.get("note") or "",
        key=f"admin_edit_note_{safe}",
        height=200,  # 正文域：medium=200px（Streamlit 1.59.1 只接受正整数像素）
    )
    facts = st.text_area(
        t("admin_persons_facts"),
        value="\n".join(existing_facts),
        key=f"admin_edit_facts_{safe}",
        height=200,  # 事实域：medium=200px（Streamlit 1.59.1 只接受正整数像素）
    )
    pinned_box = st.checkbox(
        t("admin_persons_pin"),
        value=bool(meta.get("pinned")),
        key=f"admin_edit_pinned_{safe}",
    )
    st.divider()
    if st.button(t("admin_persons_save"), key=f"admin_edit_save_{safe}", type="primary", width="stretch"):
        fact_list = [f.strip() for f in facts.splitlines() if f.strip()]
        person_store.update_person_meta(
            name,
            dynasty=dynasty.strip(),
            category=category.strip(),
            note=note.strip(),
            facts=fact_list,
            pinned=pinned_box,
        )
        st.toast(t("admin_persons_saved"))
        st.session_state.pop("admin_person_editor_open", None)
        st.rerun()


def render_persons_new_section() -> None:
    with st.expander(t("admin_persons_new"), key="admin_persons_new_exp"):
        col = st.columns(2, vertical_alignment="top")
        with col[0]:
            name = st.text_input(
                t("admin_persons_name"),
                key="admin_person_new_name",
                placeholder=t("admin_persons_name_ph"),
            )
            dynasty = st.text_input(t("admin_persons_dynasty"), key="admin_person_new_dynasty")
            category = st.text_input(t("admin_persons_category"), key="admin_person_new_category")
        with col[1]:
            note = st.text_area(t("admin_persons_note"), key="admin_person_new_note", height=80)  # 新建页紧凑域：small=80px
            facts = st.text_area(t("admin_persons_facts"), key="admin_person_new_facts", height=80)
        with st.container(key="admin_persons_new_action"):
            if st.button(t("admin_persons_create"), key="admin_persons_new_create", type="primary", width="stretch", disabled=not name.strip()):
                ok = person_store.create_custom_person(
                    name.strip(),
                    dynasty=dynasty.strip(),
                    category=category.strip(),
                    note=note.strip(),
                    facts=[f.strip() for f in facts.splitlines() if f.strip()],
                )
                if ok:
                    st.toast(t("admin_persons_created"))
                    st.rerun()
                else:
                    st.error(t("admin_persons_create_failed"))


def _dynasty_options():
    try:
        return person_store.dynasty_options() or []
    except Exception:
        return []


def _category_options():
    try:
        return person_store.category_options() or []
    except Exception:
        return []


def _group_names():
    try:
        return [g.get("name") for g in person_store.list_groups()]
    except Exception:
        return []


# 安全：__all__ 暴露
render_persons_page_full = render_persons_page
render_persons_new = render_persons_new_section
