# webui/tasks.py
import streamlit as st
import requests
from loguru import logger
from webui.base import (VOICE_MODE_NONE,VOICE_MODE_TTS,VOICE_MODE_UPLOAD,_DOWNLOAD_FILENAME_INVALID_PATTERN,_FINAL_VIDEO_PATTERN,_WINDOWS_RESERVED_FILENAMES,_is_llm_configured,_save_runtime_config,_set_runtime_config,locales,localized_widget_key,root_dir,tr,tr_optional)


import html
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import webbrowser
from collections.abc import Mapping
from datetime import datetime
from uuid import UUID, uuid4
from uuid import uuid4
from app.models import const
from app.config import config
from app.models.llm_provider import (get_llm_provider)
from app.models.schema import (VideoAspect,VideoFitMode,VideoParams,VideoTransitionMode)
from app.services import (llm,material,state as sm,task as tm,version_checker,voice,webui_task)
from app.utils import utils

def _format_task_time(timestamp):
    if not timestamp:
        return "-"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")

def _format_task_subject(subject, max_length=30):
    subject = str(subject or "").replace("\n", " ").strip()
    if len(subject) <= max_length:
        return subject or "-"
    return f"{subject[:max_length]}..."

def _safe_load_task_script(task_path):
    script_file = os.path.join(task_path, "script.json")
    if not os.path.isfile(script_file):
        return {}

    try:
        with open(script_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"failed to read task script data: {script_file}, {e}")
        return {}

def _find_final_task_video(task_path: str) -> str:
    """
    返回任务目录中序号最小的最终成片。

    合成流程还会产生 combined、temp-clip 和 MoviePy 临时文件，这些文件不能
    表示任务已成功完成，因此这里只接受 ``final-<序号>.<扩展名>``。
    """
    try:
        files = os.listdir(task_path)
    except OSError:
        return ""

    candidates = []
    for file_name in files:
        match = _FINAL_VIDEO_PATTERN.fullmatch(file_name)
        if match:
            candidates.append((int(match.group("index")), file_name))

    if not candidates:
        return ""

    _, file_name = min(candidates, key=lambda item: item[0])
    return os.path.join(task_path, file_name)

def _build_restore_upload_requirements(params: Mapping) -> dict:
    """
    记录历史任务中无法由 Streamlit 自动恢复的上传文件依赖。

    浏览器不允许程序重新填充 file_uploader，因此恢复任务时需要单独记录本地
    素材和自定义音频依赖，并在用户重新生成前检查是否已经主动补充或替换。
    """
    return {
        "local_materials": params.get("video_source") == "local",
        "custom_audio": bool(params.get("custom_audio_file")),
        "original_voice_name": params.get("voice_name") or "",
    }

def _get_unmet_restore_upload_requirements(
    requirements: Mapping | None,
    *,
    video_source: str,
    voice_name: str,
    has_local_materials: bool,
    has_custom_audio: bool,
    voice_mode: str | None = None,
) -> set[str]:
    """返回当前表单仍未满足的历史上传文件依赖。"""
    requirements = requirements or {}
    unmet = set()

    if (
        requirements.get("local_materials")
        and video_source == "local"
        and not has_local_materials
    ):
        unmet.add("local_materials")

    if requirements.get("custom_audio") and not has_custom_audio:
        if voice_mode is not None:
            # 新版 WebUI 使用显式配音方式。用户切换到自动配音或无配音，表示
            # 已主动替换历史上传音频；只有继续选择上传模式时才要求重新上传。
            if voice_mode == VOICE_MODE_UPLOAD:
                unmet.add("custom_audio")
        elif voice_name == requirements.get("original_voice_name", ""):
            # 保留旧调用方按音色判断的兼容行为，避免影响 API 和已有测试工具。
            unmet.add("custom_audio")

    return unmet

def _queue_task_restore(task_id):
    # 任务列表运行在 fragment 中，不能直接修改已经创建的主表单控件状态。
    # 这里只记录候选任务并触发整页 rerun，确认和参数恢复由主页面统一处理。
    st.session_state["task_restore_candidate_id"] = task_id
    st.session_state["task_manager_popover_nonce"] = (
        st.session_state.get("task_manager_popover_nonce", 0) + 1
    )
    st.rerun(scope="app")

def _normalize_task_state(state):
    if state in (
        const.TASK_STATE_COMPLETE,
        const.TASK_STATE_FAILED,
        const.TASK_STATE_PROCESSING,
    ):
        return state
    try:
        return int(state)
    except (TypeError, ValueError):
        return state

def _active_generation_tasks():
    tasks = st.session_state.setdefault("active_generation_tasks", {})
    if not isinstance(tasks, dict):
        tasks = {}
        st.session_state["active_generation_tasks"] = tasks
    return tasks

def _add_active_generation_task(task_id, subject=None):
    tasks = _active_generation_tasks()
    task = tasks.setdefault(task_id, {})
    task["subject"] = subject or task.get("subject") or task_id
    task["mtime"] = task.get("mtime") or datetime.now().timestamp()

def _remove_active_generation_task(task_id):
    tasks = _active_generation_tasks()
    if task_id in tasks:
        del tasks[task_id]
    if st.session_state.get("pending_generation_task_id") == task_id:
        del st.session_state["pending_generation_task_id"]

def _prepare_generation_task():
    # st.button 的 on_click 会在页面脚本重新执行前触发。这里提前生成任务 ID，
    # 顶部任务管理入口就能在同一次 rerun 中显示“生成中”数量。
    task_id = str(uuid4())
    st.session_state["pending_generation_task_id"] = task_id
    subject = st.session_state.get("video_subject") or st.session_state.get(
        "video_script"
    )
    _add_active_generation_task(task_id, subject=subject)

def _task_state_label(state, has_video):
    normalized_state = _normalize_task_state(state)
    if normalized_state == const.TASK_STATE_COMPLETE:
        return tr("Task Status Complete")
    if normalized_state == const.TASK_STATE_FAILED:
        return tr("Task Status Failed")
    if normalized_state == const.TASK_STATE_PROCESSING:
        return tr("Task Status Processing")
    if has_video:
        return tr("Task Status Complete")
    return tr("Task Status History")

def _task_state_filter_key(task):
    normalized_state = _normalize_task_state(task.get("state"))
    if normalized_state == const.TASK_STATE_PROCESSING:
        return "processing"
    if normalized_state == const.TASK_STATE_FAILED:
        return "failed"
    if normalized_state == const.TASK_STATE_COMPLETE or task["video_file"]:
        return "complete"
    return "history"

def _scan_history_tasks(limit=30):
    tasks_root = utils.task_dir()
    if not os.path.isdir(tasks_root):
        return []

    # 任务管理 fragment 每两秒刷新一次。先只读取低成本的目录元数据并截取最近
    # 的任务，再解析 script.json 和视频列表，避免历史任务很多时反复扫描全部内容。
    task_entries = []
    try:
        with os.scandir(tasks_root) as entries:
            for entry in entries:
                try:
                    if entry.name.startswith(".") or not entry.is_dir(
                        follow_symlinks=False
                    ):
                        continue
                    task_entries.append(
                        (
                            entry.stat(follow_symlinks=False).st_mtime,
                            entry.name,
                            entry.path,
                        )
                    )
                except OSError as e:
                    # 单个任务目录可能正在被删除，不应因此让整个任务面板失效。
                    logger.debug(f"skip unavailable task directory: {entry.path}, {e}")
    except OSError as e:
        logger.warning(f"failed to scan task directory: {tasks_root}, {e}")
        return []

    task_entries.sort(key=lambda item: item[0], reverse=True)
    tasks = []
    for mtime, name, task_path in task_entries[:limit]:
        script_data = _safe_load_task_script(task_path)
        params_data = script_data.get("params", {}) if script_data else {}
        video_file = _find_final_task_video(task_path)
        subject = (
            params_data.get("video_subject")
            or script_data.get("script", "")[:40]
            or name
        )
        tasks.append(
            {
                "task_id": name,
                "subject": subject,
                "state": const.TASK_STATE_COMPLETE if video_file else None,
                "progress": 100 if video_file else 0,
                "mtime": mtime,
                "task_path": task_path,
                "video_file": video_file,
                "source": "history",
            }
        )

    return tasks

def _collect_task_summaries(limit=20):
    history_tasks = {task["task_id"]: task for task in _scan_history_tasks(limit=50)}

    try:
        runtime_tasks, _ = sm.state.get_all_tasks(1, 50)
    except Exception as e:
        logger.warning(f"failed to load runtime tasks: {e}")
        runtime_tasks = []

    for task in runtime_tasks:
        task_id = task.get("task_id", "")
        if not task_id:
            continue

        task_path = os.path.join(utils.task_dir(), task_id)
        history_task = history_tasks.get(task_id, {})
        video_files = task.get("videos") or []
        video_file = (
            video_files[0] if video_files else history_task.get("video_file", "")
        )
        subject = (
            task.get("video_subject")
            or history_task.get("subject")
            or (task.get("script", "")[:40] if task.get("script") else "")
            or task_id
        )

        history_tasks[task_id] = {
            "task_id": task_id,
            "subject": subject,
            "state": task.get("state"),
            "cross_post_state": task.get("cross_post_state"),
            "progress": int(task.get("progress", 0) or 0),
            "mtime": os.path.getmtime(task_path)
            if os.path.isdir(task_path)
            else history_task.get("mtime", 0),
            "task_path": task_path,
            "video_file": video_file,
            "source": "runtime",
        }

    for task_id, active_task in _active_generation_tasks().items():
        history_task = history_tasks.get(task_id, {})
        if history_task and _task_state_filter_key(history_task) in {
            "complete",
            "failed",
        }:
            # 会话中的 active 标记只负责覆盖任务刚提交到状态存储前的极短窗口。
            # 后台任务结束后必须以真实终态为准，不能把失败任务重新显示为生成中。
            continue

        task_path = os.path.join(utils.task_dir(), task_id)
        history_tasks[task_id] = {
            "task_id": task_id,
            "subject": active_task.get("subject")
            or history_task.get("subject")
            or task_id,
            "state": const.TASK_STATE_PROCESSING,
            "progress": history_task.get("progress", 0),
            "mtime": active_task.get("mtime")
            or history_task.get("mtime", datetime.now().timestamp()),
            "task_path": task_path,
            "video_file": history_task.get("video_file", ""),
            "source": "active",
        }

    tasks = list(history_tasks.values())
    return sorted(tasks, key=lambda item: item["mtime"], reverse=True)[:limit]

def _is_headless_server():
    # Docker 或无桌面的服务器部署中，WebUI 进程接触不到用户的桌面环境：
    # xdg-open / webbrowser 只会在容器内静默失败。此时应改为浏览器内预览
    # 视频、以路径提示代替打开目录。macOS/Windows 桌面部署不受影响。
    if sys.platform == "darwin" or sys.platform.startswith("win"):
        return False
    return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

def _open_task_path(task_path):
    tasks_root = os.path.abspath(utils.task_dir())
    normalized_path = os.path.abspath(task_path)
    if not normalized_path.startswith(tasks_root + os.sep):
        logger.warning(f"invalid task folder path: {normalized_path}")
        return
    if not os.path.isdir(normalized_path):
        return
    if _is_headless_server():
        # storage 目录通常以卷挂载映射回宿主机，提示相对路径即可定位文件。
        rel_path = os.path.relpath(normalized_path, os.path.dirname(tasks_root))
        st.toast(f"{tr('Open Task Folder')}: ./storage/{rel_path}", icon="📂")
        return
    webbrowser.open(f"file://{normalized_path}")

def _open_task_video(video_file):
    tasks_root = os.path.abspath(utils.task_dir())
    normalized_file = os.path.abspath(video_file)

    # 视频路径来自任务目录扫描或运行期状态。这里仍然限制只能打开任务目录
    # 内的文件，避免 UI 操作被异常路径扩展成任意本地文件打开能力。
    if not normalized_file.startswith(tasks_root + os.sep):
        logger.warning(f"invalid task video path: {normalized_file}")
        return
    if not os.path.isfile(normalized_file):
        logger.warning(f"task video does not exist: {normalized_file}")
        return

    if _is_headless_server():
        # 无桌面环境时在任务面板内嵌播放器预览，代替调用系统播放器。
        st.session_state["task_preview_video_file"] = normalized_file
        return

    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", normalized_file])
        elif sys.platform.startswith("win"):
            os.startfile(normalized_file)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", normalized_file])
    except Exception as e:
        logger.error(f"failed to open task video: {normalized_file}, {e}")

def _delete_task(task_id, task_path, task_state=None):
    # 页面展示的状态可能落后于后台任务。删除前同时检查传入状态、当前会话的
    # 活跃任务和最新状态，避免任务刚开始或已产出中间视频时被误删。
    current_task = None
    try:
        current_task = sm.state.get_task(task_id)
    except Exception as e:
        logger.exception(f"failed to verify task state before deletion: {task_id}, {e}")
        return False

    task_snapshot = dict(current_task or {})
    task_snapshot.setdefault("state", task_state)
    if task_id in _active_generation_tasks():
        task_snapshot["state"] = const.TASK_STATE_PROCESSING

    if tm.is_task_busy(task_snapshot):
        logger.warning(f"refused to delete running task: {task_id}")
        return False

    tasks_root = os.path.abspath(utils.task_dir())
    normalized_path = os.path.abspath(task_path)

    # 删除任务会移除任务状态和本地生成文件。这里必须限定在 storage/tasks
    # 下，避免异常 task_path 造成误删其它本地目录。
    if not normalized_path.startswith(tasks_root + os.sep):
        logger.warning(f"invalid task folder path for deletion: {normalized_path}")
        return False

    try:
        if hasattr(sm.state, "delete_task"):
            sm.state.delete_task(task_id)
        if os.path.isdir(normalized_path):
            shutil.rmtree(normalized_path)
        logger.info(f"deleted task: {task_id}")
        return True
    except Exception as e:
        logger.exception(f"failed to delete task: {task_id}, {e}")
        return False

def _count_processing_tasks(tasks):
    # 顶部任务管理入口只需要展示“生成中”任务数量。
    # 这里复用内部状态 key 判断，避免依赖多语言展示文案导致不同语言下统计不一致。
    processing_task_ids = {
        task["task_id"]
        for task in tasks
        if _task_state_filter_key(task) == "processing"
    }
    return len(processing_task_ids)

def _task_manager_label(processing_count):
    label = tr("Task Manager")
    if processing_count <= 0:
        return label
    return f"{label} · {processing_count}"

def _build_video_download_name(subject, index, total):
    """根据视频主题生成跨平台安全的下载文件名。"""
    safe_subject = _DOWNLOAD_FILENAME_INVALID_PATTERN.sub(" ", str(subject or ""))
    safe_subject = re.sub(r"\s+", " ", safe_subject).strip(" .")[:80].rstrip(" .")
    if not safe_subject:
        safe_subject = "video"
    # Win32 在识别设备名时会忽略扩展名前的尾随空格和句点。与背景音乐上传
    # 的现有规则保持一致，避免 ``CON .topic`` 绕过保留名保护。
    windows_basename = safe_subject.split(".", 1)[0].rstrip(" .").upper()
    if windows_basename in _WINDOWS_RESERVED_FILENAMES:
        safe_subject = f"_{safe_subject}"

    suffix = f"-{index}" if total > 1 else ""
    return f"{safe_subject}{suffix}.mp4"

def _render_task_table(filtered_tasks, key_prefix):
    with st.container(key=f"task_table_header_{key_prefix}"):
        header_cols = st.columns([1.1, 1.7, 3.0, 0.8, 1.6], vertical_alignment="center")
        header_cols[0].caption(tr("Task Status"))
        header_cols[1].caption(tr("Task Updated At"))
        header_cols[2].caption(tr("Task Subject"))
        header_cols[3].caption(tr("Task Progress"))
        header_cols[4].caption(tr("Task Actions"))

    if not filtered_tasks:
        st.info(tr("No Tasks Match Filter"))
        return

    visible_tasks = filtered_tasks[:12]
    list_height = min(390, max(96, len(visible_tasks) * 58))
    with st.container(height=list_height, border=False):
        for task in visible_tasks:
            task_id = task["task_id"]
            has_video = bool(task["video_file"] and os.path.isfile(task["video_file"]))
            is_processing = _task_state_filter_key(task) == "processing"
            is_busy = is_processing or tm.is_task_busy(task)
            has_restore_data = os.path.isfile(
                os.path.join(task["task_path"], "script.json")
            )
            safe_task_key = "".join(ch if ch.isalnum() else "_" for ch in task_id)[:40]

            # 使用 Streamlit 原生 bordered container + columns 保留每行操作。
            # 相比自定义 HTML/CSS 表格，这种方式对 Streamlit 版本变更更稳；
            # 相比 dataframe，又能保留播放、打开目录、删除等行内动作。
            with st.container(
                key=f"task_row_{key_prefix}_{safe_task_key}", border=True
            ):
                row_cols = st.columns(
                    [1.1, 1.7, 3.0, 0.8, 1.6],
                    vertical_alignment="center",
                )
                row_cols[0].write(_task_state_label(task["state"], has_video))
                row_cols[1].write(_format_task_time(task["mtime"]))
                row_cols[2].write(_format_task_subject(task["subject"]))
                row_cols[3].write(f"{task['progress']}%")

                action_cols = row_cols[4].columns(
                    4,
                    vertical_alignment="center",
                    gap="small",
                )
                with action_cols[0]:
                    play_label = tr("Play")
                    if st.button(
                        play_label,
                        key=f"play_task_{key_prefix}_{task_id}",
                        width="stretch",
                        icon=":material/play_arrow:",
                        help=play_label,
                        disabled=not has_video,
                    ):
                        _open_task_video(task["video_file"])

                with action_cols[1]:
                    open_label = tr("Open Task Folder")
                    if st.button(
                        open_label,
                        key=f"open_task_{key_prefix}_{task_id}",
                        width="stretch",
                        icon=":material/folder_open:",
                        help=open_label,
                    ):
                        _open_task_path(task["task_path"])

                with action_cols[2]:
                    restore_label = tr("Regenerate Task")
                    if st.button(
                        restore_label,
                        key=f"restore_task_{key_prefix}_{task_id}",
                        width="stretch",
                        icon=":material/replay:",
                        help=restore_label,
                        disabled=is_processing or not has_restore_data,
                    ):
                        _queue_task_restore(task_id)

                with action_cols[3]:
                    delete_label = tr("Delete Task")
                    delete_help = (
                        f"{delete_label} ({tr('Task Status Processing')})"
                        if is_busy
                        else delete_label
                    )
                    if st.button(
                        delete_label,
                        key=f"delete_task_{key_prefix}_{task_id}",
                        width="stretch",
                        icon=":material/delete:",
                        help=delete_help,
                        disabled=is_busy,
                    ):
                        if _delete_task(task_id, task["task_path"], task["state"]):
                            st.toast(tr("Task Deleted"))
                            st.rerun()
                        else:
                            st.error(tr("Task Delete Failed"))

def _render_task_manager_panel(tasks=None):
    tasks = tasks if tasks is not None else _collect_task_summaries()
    if not tasks:
        st.info(tr("No Tasks Yet"))
        return

    # Streamlit 1.59 支持有状态 Tabs 的惰性渲染。切换时只重新构建当前列表，
    # 避免定时 Fragment 每两秒重复创建四套任务行和操作按钮。
    status_tabs = [
        ("all", tr("All Tasks")),
        ("processing", tr("Task Status Processing")),
        ("complete", tr("Task Status Complete")),
        ("failed", tr("Task Status Failed")),
    ]
    tabs = st.tabs(
        [label for _, label in status_tabs],
        key="task_manager_status_tabs",
        on_change="rerun",
    )
    for (status_key, _), tab in zip(status_tabs, tabs):
        if not tab.open:
            continue
        with tab:
            filtered_tasks = [
                task
                for task in tasks
                if status_key == "all" or _task_state_filter_key(task) == status_key
            ]
            _render_task_table(filtered_tasks, status_key)

    _render_task_video_preview()

def _render_task_video_preview():
    # 无桌面部署下“播放”按钮的浏览器内回退：在任务面板底部渲染播放器。
    preview_file = st.session_state.get("task_preview_video_file")
    if not preview_file:
        return

    tasks_root = os.path.abspath(utils.task_dir())
    if not (
        preview_file.startswith(tasks_root + os.sep) and os.path.isfile(preview_file)
    ):
        st.session_state.pop("task_preview_video_file", None)
        return

    st.divider()
    preview_cols = st.columns([5, 1], vertical_alignment="center")
    task_name = os.path.basename(os.path.dirname(preview_file))
    preview_cols[0].caption(f"{os.path.basename(preview_file)} · {task_name}")
    closed = preview_cols[1].button(
        "✕",
        key="close_task_video_preview",
        width="stretch",
        help=tr("Close"),
    )
    if closed:
        st.session_state.pop("task_preview_video_file", None)
        return
    st.video(preview_file)

@st.fragment(run_every="2s")
def _render_task_manager_entry():
    # 任务可能由当前页面或其它页面触发生成。入口单独用 fragment 定时刷新，
    # 只更新任务数量和 popover 内容，不打断主页面表单输入。
    task_summaries = _collect_task_summaries()
    processing_task_count = _count_processing_tasks(task_summaries)
    with st.container(key="task_manager_entry", width="content"):
        with st.popover(
            _task_manager_label(processing_task_count),
            width="content",
            key=(
                "task_manager_popover_"
                f"{st.session_state.get('task_manager_popover_nonce', 0)}"
            ),
        ):
            _render_task_manager_panel(task_summaries)

def _load_task_restore_payload(task_id):
    tasks_root = os.path.realpath(utils.task_dir())
    task_path = os.path.realpath(os.path.join(tasks_root, str(task_id)))
    try:
        if os.path.commonpath([tasks_root, task_path]) != tasks_root:
            raise ValueError("task path is outside the task directory")
    except ValueError as e:
        logger.warning(f"invalid task restore path: {task_id}, {e}")
        return None

    script_data = _safe_load_task_script(task_path)
    raw_params = script_data.get("params")
    if not isinstance(raw_params, dict):
        logger.warning(f"task has no restorable parameters: {task_id}")
        return None

    params_input = dict(raw_params)
    if script_data.get("script"):
        params_input["video_script"] = script_data["script"]
    if script_data.get("search_terms"):
        params_input["video_terms"] = script_data["search_terms"]

    try:
        params = VideoParams.model_validate(params_input).model_dump(mode="json")
    except Exception as e:
        logger.warning(f"failed to validate task restore parameters: {task_id}, {e}")
        return None

    return {
        "task_id": str(task_id),
        "subject": params.get("video_subject") or script_data.get("script") or task_id,
        "params": params,
    }

def _infer_tts_server_from_voice(voice_name):
    if voice.is_no_voice(voice_name):
        return voice.NO_VOICE_NAME
    if voice.is_siliconflow_voice(voice_name):
        return "siliconflow"
    if voice.is_gemini_voice(voice_name):
        return "gemini-tts"
    if voice.is_mimo_voice(voice_name):
        return "mimo-tts"
    if voice.is_minimax_voice(voice_name):
        return "minimax-tts"
    if voice.is_elevenlabs_voice(voice_name):
        return "elevenlabs"
    if voice.is_chatterbox_voice(voice_name):
        return "chatterbox"
    if voice.is_kokoro_voice(voice_name):
        return "kokoro"
    if voice.is_fish_audio_voice(voice_name):
        return "fish_audio"
    if voice.is_voxcpm_voice(voice_name):
        return "voxcpm"
    if voice.is_azure_v2_voice(voice_name):
        return "azure-tts-v2"
    return "azure-tts-v1"

def _set_stable_widget_value(key, value):
    if value is not None:
        st.session_state[localized_widget_key(key)] = value

def _apply_pending_task_restore():
    payload = st.session_state.pop("task_restore_payload", None)
    if not payload:
        return False

    _apply_restored_params(payload["params"])
    st.session_state["task_restore_succeeded"] = True
    logger.info(f"restored task configuration: {payload['task_id']}")
    return True

def _apply_restored_params(params):
    """
    把一份完整的生成参数写回页面控件状态。

    历史任务恢复和设置预设导入使用同一份参数模型，因此共用同一个实现，避免
    新增字段时只更新其中一条路径。调用方必须在渲染任何控件之前执行，否则
    Streamlit 会拒绝修改已经实例化的控件状态。
    """
    video_terms = params.get("video_terms") or ""
    if isinstance(video_terms, list):
        video_terms = ", ".join(str(term) for term in video_terms)

    # 文案与高级脚本设置。
    st.session_state["video_subject"] = params.get("video_subject") or ""
    st.session_state["video_script"] = params.get("video_script") or ""
    st.session_state["video_terms"] = str(video_terms)
    _set_stable_widget_value(
        "script_language_select", params.get("video_language") or ""
    )
    # 传记模式：恢复人物到向导步骤①的人物选择控件。若只恢复 video_subject，
    # 向导①的 selectbox 仍会保留用户此前选过的人物（前端上报的旧值会覆盖
    # 参数），导致"重新生成"后实际是另一个人的视频。必须在控件重建前写入。
    biography_person = str(params.get("biography_person") or "").strip()
    if biography_person:
        # 1) 选中该人物（搜索词 + 重置下拉选中）
        st.session_state["biography_person_input"] = biography_person
        st.session_state["library_person_search"] = biography_person
        # 重置下拉框为默认（索引 0），避免仍停在之前选中的人物
        st.session_state.pop("library_person_select", None)
        # 2) 清空朝代/类别/分组筛选，防止旧的筛选排除掉要恢复的人物
        for _fk in ("library_dynasty_filter", "library_category_filter", "library_group_filter"):
            st.session_state.pop(_fk, None)
        _set_runtime_config("app", "biography_person", biography_person)
    st.session_state["paragraph_number_input"] = params.get("paragraph_number", 1)
    st.session_state["video_script_prompt"] = params.get("video_script_prompt") or ""
    st.session_state["custom_system_prompt"] = (
        params.get("custom_system_prompt") or llm.DEFAULT_SCRIPT_SYSTEM_PROMPT
    )

    # 视频设置。素材上传控件不能由服务端写入，因此本地素材需要用户重新选择。
    video_source = params.get("video_source") or "pexels"
    _set_stable_widget_value("video_source_select", video_source)
    _set_stable_widget_value(
        "video_concat_mode_select", params.get("video_concat_mode") or "random"
    )
    _set_stable_widget_value(
        "video_transition_mode_select",
        params.get("video_transition_mode") or VideoTransitionMode.none.value,
    )
    _set_stable_widget_value(
        f"video_aspect_for_{video_source}",
        params.get("video_aspect") or VideoAspect.portrait.value,
    )
    _set_stable_widget_value(
        "video_fit_mode_select",
        params.get("video_fit_mode") or VideoFitMode.cover.value,
    )
    _set_stable_widget_value(
        "video_clip_duration_select", params.get("video_clip_duration", 3)
    )
    _set_stable_widget_value(
        "video_clip_speed_slider",
        # API 可以写入超过 WebUI 范围的速度，任务生成阶段会安全归一化，但
        # 历史记录仍可能保留原值。恢复任务前再次归一化，避免给 Streamlit
        # slider 注入越界值、NaN 或无穷值导致控件状态异常。
        utils.normalize_clip_speed(params.get("video_clip_speed", 1.0)),
    )
    _set_stable_widget_value("video_count_select", params.get("video_count", 1))
    st.session_state["match_materials_to_script"] = bool(
        params.get("match_materials_to_script", False)
    )

    # 音频设置。TTS server 未写入旧任务，根据历史 voice_name 推断。
    voice_name = params.get("voice_name") or voice.NO_VOICE_NAME
    tts_server = _infer_tts_server_from_voice(voice_name)
    if params.get("custom_audio_file"):
        voice_mode = VOICE_MODE_UPLOAD
    elif voice.is_no_voice(voice_name):
        voice_mode = VOICE_MODE_NONE
    else:
        voice_mode = VOICE_MODE_TTS
    _set_stable_widget_value("voice_mode_control", voice_mode)
    if tts_server != voice.NO_VOICE_NAME:
        _set_stable_widget_value("tts_server_select", tts_server)
        _set_stable_widget_value(f"speech_synthesis_select_{tts_server}", voice_name)
    _set_stable_widget_value("voice_volume_select", params.get("voice_volume", 1.0))
    _set_stable_widget_value("voice_rate_select", params.get("voice_rate", 1.0))
    bgm_type = params.get("bgm_type") or ""
    _set_stable_widget_value("bgm_type_select", bgm_type)
    _set_stable_widget_value("bgm_volume_select", params.get("bgm_volume", 0.2))
    if bgm_type == "preset" and params.get("bgm_file"):
        # 预设歌曲控件使用文件名作为稳定业务值。历史任务可能保存绝对路径或
        # 相对路径，统一取 basename 后即可匹配当前安全枚举出的歌曲列表。
        _set_stable_widget_value(
            "preset_song_select", os.path.basename(str(params["bgm_file"]))
        )
    st.session_state["custom_bgm_file_input"] = params.get("bgm_file") or ""
    st.session_state["sonilo_bgm_prompt_input"] = (
        params.get("video_music_prompt") or params.get("sonilo_bgm_prompt") or ""
    )
    st.session_state["elevenlabs_music_prompt_input"] = (
        params.get("video_music_prompt") or ""
    )

    # 字幕设置。对旧任务中的越界数值做最小限幅，避免 Slider 无法初始化。
    st.session_state["subtitle_enabled_checkbox"] = bool(
        params.get("subtitle_enabled", True)
    )
    _set_stable_widget_value("font_name_select", params.get("font_name") or "")
    _set_stable_widget_value(
        "subtitle_position_select", params.get("subtitle_position") or "bottom"
    )
    _set_stable_widget_value(
        "subtitle_display_mode_select", params.get("subtitle_display_mode") or "sentence"
    )
    _set_stable_widget_value(
        "subtitle_animation_select", params.get("subtitle_animation") or "none"
    )
    custom_position = min(100.0, max(0.0, float(params.get("custom_position", 70.0))))
    st.session_state["custom_position_input"] = str(custom_position)
    st.session_state["font_color_picker"] = params.get("text_fore_color") or "#FFFFFF"
    st.session_state["font_size_slider"] = min(
        100, max(30, int(params.get("font_size", 60)))
    )
    st.session_state["stroke_color_picker"] = params.get("stroke_color") or "#000000"
    st.session_state["stroke_width_slider"] = min(
        10.0, max(0.0, float(params.get("stroke_width", 1.5)))
    )
    background_color = params.get("text_background_color")
    background_enabled = bool(background_color)
    st.session_state["subtitle_background_enabled_checkbox"] = background_enabled
    if isinstance(background_color, str):
        st.session_state["subtitle_background_color_picker"] = background_color
    st.session_state["rounded_subtitle_background_checkbox"] = bool(
        params.get("rounded_subtitle_background", False) and background_enabled
    )

    st.session_state.pop("local_video_materials_uploader", None)
    # 历史任务只保存素材路径，不能保证这些文件在当前环境仍然存在。
    # 同时清空当前页面已缓存的上传素材，避免恢复后误用另一个任务的文件。
    st.session_state["local_video_materials"] = []
    st.session_state.pop("custom_audio_file_uploader", None)
    st.session_state.pop("custom_bgm_uploader", None)
    st.session_state.pop("custom_bgm_validation", None)
    st.session_state["task_restore_upload_requirements"] = (
        _build_restore_upload_requirements(params)
    )

    return True

def _dismiss_task_restore_dialog():
    st.session_state.pop("task_restore_candidate_id", None)

@st.dialog(
    tr("Regenerate Task"),
    width="small",
    on_dismiss=_dismiss_task_restore_dialog,
)
def _render_task_restore_dialog(task_id):
    payload = _load_task_restore_payload(task_id)
    if payload is None:
        st.error(tr("Task Restore Failed"))
        if st.button(tr("Cancel"), key="cancel_invalid_task_restore"):
            st.session_state.pop("task_restore_candidate_id", None)
            st.rerun(scope="app")
        return

    st.write(tr("Regenerate Task Confirmation"))
    st.caption(_format_task_subject(payload["subject"], max_length=80))
    cancel_col, load_col = st.columns(2)
    if cancel_col.button(
        tr("Cancel"),
        key="cancel_task_restore",
        width="stretch",
    ):
        st.session_state.pop("task_restore_candidate_id", None)
        st.rerun(scope="app")
    if load_col.button(
        tr("Load Task Configuration"),
        key="confirm_task_restore",
        type="primary",
        width="stretch",
    ):
        st.session_state["task_restore_payload"] = payload
        st.session_state.pop("task_restore_candidate_id", None)
        st.rerun(scope="app")

def _dismiss_settings_dialog():
    """关闭设置弹窗，并确保下一次整页 rerun 不会再次自动打开。"""
    st.session_state["settings_dialog_open"] = False

def _open_settings_dialog(target_tab=None):
    """打开设置弹窗，并可直接定位到指定业务标签页。"""
    st.session_state["settings_dialog_open"] = True
    if target_tab:
        # 这里只保存稳定的业务 ID，不保存翻译文本；真正创建 tabs 前再根据
        # 当前界面语言解析 label，避免用户切换语言后旧文案成为非法选项。
        st.session_state["settings_dialog_target_tab"] = target_tab

def _open_settings_dialog_from_onboarding():
    """首次使用引导里的“去配置”按钮：只打开 LLM 设置弹窗，不改变 admin_page。
    用户完成配置后，_is_llm_configured 自然变为 True，引导不再出现（持久化 + 状态双重保证）。"""
    _open_settings_dialog(target_tab="llm")

def _render_first_run_onboarding():
    """非模态首次使用引导（替代原遮挡式 streamlit_tour）。

    产品设计原则（产品经理视角）：
      1. 零打扰成熟用户：只在「新用户且尚未配置可用 LLM Key」时出现；
         已配好 Key 的成熟用户完全无感。
      2. 非模态、不拦截：不遮挡主表单、不拦截任何点击/输入，
         用户想直接生成视频照样能点。
      3. 给价值而非讲概念：一句话说明「需要先配一个 AI 模型」，
         一个明确动作“去配置”，把用户送到能真正解决问题的 LLM 设置页。
      4. 一次性：同一浏览器会话内点掉后不再反复弹出（session 标志）；
         即使不点，只要完成 LLM 配置就永久不再出现。

    该提示仅出现在生成视频页（_render_generation_controls）；侧边栏管理页
    （任务/存储/资料库/人物）不受任何引导干扰。

    另支持 MPT_FORCE_ONBOARDING=1 环境变量强制显示，便于开发测试或用户
    在已配 Key 的环境下再次查看此提示。
    """
    # 强制显示开关（测试 / 重新触发）
    force = os.environ.get("MPT_FORCE_ONBOARDING") == "1"

    # 已配置可用 LLM Key → 成熟用户，零打扰（强制模式除外）
    if _is_llm_configured() and not force:
        return

    # 本会话内已点过“知道了” → 不再重复（点掉即干净退出，同 session 内不扰）
    if st.session_state.get("first_run_onboarding_dismissed", False):
        return

    with st.container(border=True):
        col_a, col_b, col_c = st.columns([5.2, 1.6, 1.2], vertical_alignment="center")
        with col_a:
            st.markdown(
                f"💡 {tr('First Run Onboarding Title')}.\n\n"
                f"{tr('First Run Onboarding Desc')}",
                unsafe_allow_html=True,
            )
        with col_b:
            if st.button(
                tr("First Run Onboarding Open Settings"),
                icon=":material/settings:",
                key="first_run_onboarding_open",
                on_click=_open_settings_dialog_from_onboarding,
            ):
                st.session_state["first_run_onboarding_dismissed"] = True
        with col_c:
            if st.button(
                tr("First Run Onboarding Dismiss"),
                key="first_run_onboarding_dismiss",
            ):
                # 点“知道了”就干净退出，不再反复弹（同 session 内不扰）。
                # 注意：按钮返回 True 只在“点击后那一次 rerun”里成立，而本函数开头
                # 的“已关闭”守卫读的是同一份 session_state；必须在置位后立刻
                # st.rerun()，否则当前这次 rerun 的渲染已经发生、守卫要看下次，
                # 导致用户点了但提示还挂在页面上（直到下一次操作才消失）。
                # st.rerun() 会立即重启脚本：新一次 rerun 读到 dismiss=True，
                # 开头守卫直接 return，提示立刻消失，且不会形成死循环（因为
                # 已 return，下方按钮不再被实例化，也就不会再触发 rerun）。
                st.session_state["first_run_onboarding_dismissed"] = True
                st.rerun()

def _render_person_picker(current_person: str = "") -> str:
    """向导步骤①：搜索/三维筛选并选择传主，展示档案摘要与人物管理 CRUD.

    文库 persons 表由离线管道重建，用户的分组/备注/编辑都存于
    person_store 的叠加层库，这里只读写叠加层，不碰 library.sqlite.
    勾选“基础脚本”已拆到步骤② _render_base_script_picker；本函数只负责
    人物选择，返回选中的传主姓名（无档案库或未选人时返回空串）.
    """
    try:
        from app.services import library, person_store
    except Exception as exc:  # pragma: no cover - 导入失败不应阻断页面
        st.caption(f"{tr('Local Library Unavailable')} ({type(exc).__name__})")
        return

    db = library.db_path()
    if not library.enabled() or db is None:
        st.info(tr("Local Library Not Built"))
        return

    conn = library.get_conn()
    if conn is None:
        st.warning(tr("Local Library Unavailable"))
        return

    # 向导已将本函数置于"选人物"步骤 tab 内，这里不再用折叠 expander 包裹，
    # 直接平铺主路径（搜索 + 人物下拉 + 档案 + 管理），减少一层点击.
    with st.container():
        # ---- 筛选行：朝代 | 内容类别 | 自定义分组 | 关键词 ----
        all_dynasties_label = tr("All Dynasties")
        all_categories_label = tr("All Categories")
        all_groups_label = tr("All Groups")
        try:
            dynasties = person_store.dynasty_options()
            categories = person_store.category_options()
            group_names = [g["name"] for g in person_store.list_groups()]
        except Exception as exc:  # 叠加层损坏时降级为只读文库浏览
            logger.warning(f"person store filters unavailable: {exc}")
            dynasties = []
            categories = []
            group_names = []

        filter_col_a, filter_col_b, filter_col_c, filter_col_d = st.columns(
            [1, 1, 1, 1.4]
        )
        with filter_col_a:
            # options 必须是"显示即所用"的值：Streamlit 会用显示文本反查索引.
            dynasty_filter = st.selectbox(
                tr("Dynasty"),
                options=[all_dynasties_label] + dynasties,
                key="library_dynasty_filter",
            )
            if dynasty_filter == all_dynasties_label:
                dynasty_filter = ""
        with filter_col_b:
            category_filter = st.selectbox(
                tr("Content Category"),
                options=[all_categories_label] + categories,
                key="library_category_filter",
            )
            if category_filter == all_categories_label:
                category_filter = ""
        with filter_col_c:
            group_filter = st.selectbox(
                tr("Custom Group"),
                options=[all_groups_label] + group_names,
                key="library_group_filter",
            )
            if group_filter == all_groups_label:
                group_filter = ""
        with filter_col_d:
            keyword = st.text_input(
                tr("Search Person"),
                value=current_person or "",
                key="library_person_search",
            ).strip()
            include_hidden = st.checkbox(
                tr("Show Archived"), key="library_show_archived"
            )

        try:
            rows = person_store.list_persons(
                dynasty=dynasty_filter,
                category=category_filter,
                group=group_filter,
                keyword=keyword,
                include_hidden=include_hidden,
            )
        except Exception as exc:
            logger.warning(f"person listing failed: {exc}")
            rows = []

        if not rows:
            st.caption(tr("No Person Found"))
            return

        st.caption(
            tr("Library Person Count").format(
                count=conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
            )
            + f" · {tr('Filtered Person Count').format(count=len(rows))}"
        )

        # options 必须是"显示即所用":把朝代/类别/分组拼进选项文本本身,
        # 而不是用 format_func 改写，避免反查失败.
        def _person_label(row):
            parts = [
                ("★ " if row["pinned"] else "") + row["person"],
                row["dynasty"] or "—",
                f"{tr('articles')}={row['doc_count']}",
            ]
            if row["category"]:
                parts.append(row["category"])
            if row["groups"]:
                parts.append("[" + "、".join(row["groups"]) + "]")
            if row["source"] == "custom":
                parts.append(tr("Custom Person"))
            if row["hidden"]:
                parts.append(tr("Archived"))
            return " · ".join(parts)

        labels_by_person = {}
        for r in rows:
            # 同名人物（文库+自建重名）在列表里只会合并为一条，label 取第一条.
            labels_by_person.setdefault(r["person"], _person_label(r))
        options = list(labels_by_person.values())
        label_to_person = {v: k for k, v in labels_by_person.items()}
        chosen_label = st.selectbox(
            tr("Select Person"),
            options=options,
            key="library_person_select",
        )
        chosen = label_to_person.get(chosen_label, "")
        chosen_row = next((r for r in rows if r["person"] == chosen), None)

        profile = person_store.get_person(chosen) if chosen else None
        if profile:
            badge_parts = []
            if profile.get("dynasty"):
                badge_parts.append(str(profile["dynasty"]))
            if profile.get("category"):
                badge_parts.append(str(profile["category"]))
            if profile.get("era_en"):
                badge_parts.append(f"`{profile['era_en']}`")
            st.markdown(
                f"**{profile['person']}**"
                + (" · " + " · ".join(badge_parts) if badge_parts else "")
            )
            if profile.get("groups"):
                st.caption(
                    tr("Groups") + ": " + "、".join(profile["groups"])
                )
            if profile.get("note"):
                st.markdown(f"{tr('Note')}: {profile['note']}")
            facts = profile.get("key_facts") or []
            if facts:
                st.markdown(tr("Key Facts"))
                for fact in facts[:8]:
                    st.markdown(f"- {fact}")
            # 基础脚本勾选已拆到向导步骤②（_render_base_script_picker）.
            books = profile.get("books") or []
            if books:
                st.markdown(tr("Reference Books"))
                for b in books[:5]:
                    st.markdown(f"- 《{b.get('title', '')[:40]}》")

        # ---- 操作行：选用 / 置顶 / 归档(删除) ----
        if chosen and chosen_row:
            def _select_library_person(person: str):
                # on_click 回调在本次脚本运行、传主姓名 widget 重建之前
                # 执行，此时改 session_state 才能覆盖前端持有的旧值；
                # 若在按钮分支里赋值后再 st.rerun()，会被前端上报的
                # 旧输入值覆盖回去.
                st.session_state["biography_person_input"] = person
                _set_runtime_config("app", "biography_person", person)

            action_a, action_b, action_c, _ = st.columns([1, 1, 1, 2])
            with action_a:
                if st.button(
                    tr("Use This Person"),
                    key="library_use_person",
                    on_click=_select_library_person,
                    args=(chosen,),
                ):
                    st.success(tr("Person Selected").format(person=chosen))
            with action_b:
                pin_label = (
                    tr("Unpin Person") if chosen_row["pinned"] else tr("Pin Person")
                )
                if st.button(pin_label, key="library_pin_person"):
                    person_store.update_person_meta(
                        chosen, pinned=not chosen_row["pinned"]
                    )
                    st.rerun()
            with action_c:
                if chosen_row["hidden"]:
                    if st.button(tr("Restore Person"), key="library_restore_person"):
                        person_store.restore_person(chosen)
                        st.rerun()
                else:
                    archive_label = (
                        tr("Delete Person")
                        if chosen_row["source"] == "custom"
                        else tr("Archive Person")
                    )
                    if st.button(archive_label, key="library_archive_person"):
                        result = person_store.delete_person(chosen)
                        if result == "deleted":
                            st.session_state.pop("library_person_select", None)
                        st.rerun()

            # ---- 编辑档案（仅文库人物与自建人物通用） ----
            if not chosen_row["hidden"]:
                with st.expander(tr("Edit Person"), expanded=False):
                    keep_label = tr("Keep Current")
                    clear_label = tr("Clear Override")
                    custom_label = tr("Custom Dynasty")
                    dyn_options = (
                        [keep_label, clear_label] + dynasties + [custom_label]
                    )
                    edit_col_a, edit_col_b = st.columns([1, 1])
                    with edit_col_a:
                        dyn_choice = st.selectbox(
                            tr("Dynasty Override"),
                            options=dyn_options,
                            index=0,
                            key=f"library_edit_dynasty_{chosen}",
                        )
                        dyn_custom_text = ""
                        if dyn_choice == custom_label:
                            dyn_custom_text = st.text_input(
                                tr("Dynasty Name"),
                                key=f"library_edit_dynasty_text_{chosen}",
                                max_chars=20,
                            )
                    with edit_col_b:
                        group_choice = st.multiselect(
                            tr("Custom Group"),
                            options=group_names,
                            default=chosen_row["groups"],
                            key=f"library_edit_groups_{chosen}",
                        )
                    facts_text = st.text_area(
                        tr("Key Facts"),
                        value="\n".join((profile or {}).get("key_facts") or []),
                        height=160,
                        key=f"library_edit_facts_{chosen}",
                        help=tr("Facts One Per Line Help"),
                    )
                    note_text = st.text_area(
                        tr("Note"),
                        value=(profile or {}).get("note") or "",
                        height=80,
                        key=f"library_edit_note_{chosen}",
                    )
                    if st.button(tr("Save Changes"), key="library_save_person"):
                        dynasty_value = None
                        if dyn_choice == clear_label:
                            dynasty_value = ""
                        elif dyn_choice == custom_label:
                            dynasty_value = dyn_custom_text
                        person_store.update_person_meta(
                            chosen,
                            dynasty=dynasty_value,
                            note=note_text.strip(),
                            facts=[
                                line.strip()
                                for line in facts_text.splitlines()
                                if line.strip()
                            ],
                        )
                        person_store.set_person_groups(chosen, group_choice)
                        st.success(tr("Changes Saved"))
                        st.rerun()

        # ---- 新增人物 ----
        with st.expander(tr("Add Person"), expanded=False):
            add_col_a, add_col_b = st.columns([1, 1])
            with add_col_a:
                new_name = st.text_input(
                    tr("Person Name"),
                    key="library_new_person_name",
                    max_chars=40,
                ).strip()
                new_dynasty = st.selectbox(
                    tr("Dynasty"),
                    options=[""] + dynasties,
                    key="library_new_person_dynasty",
                )
                new_category = st.selectbox(
                    tr("Content Category"),
                    options=[""] + categories,
                    key="library_new_person_category",
                )
            with add_col_b:
                new_groups = st.multiselect(
                    tr("Custom Group"),
                    options=group_names,
                    key="library_new_person_groups",
                )
            new_facts = st.text_area(
                tr("Key Facts"),
                height=120,
                key="library_new_person_facts",
                help=tr("Facts One Per Line Help"),
            )
            new_note = st.text_area(
                tr("Note"), height=80, key="library_new_person_note"
            )
            if st.button(tr("Create Person"), key="library_create_person"):
                if not new_name:
                    st.warning(tr("Please Enter Person Name"))
                else:
                    person_store.create_custom_person(
                        new_name,
                        dynasty=new_dynasty,
                        category=new_category,
                        facts=[
                            line.strip()
                            for line in new_facts.splitlines()
                            if line.strip()
                        ],
                        note=new_note.strip(),
                    )
                    if new_groups:
                        person_store.set_person_groups(new_name, new_groups)
                    st.success(tr("Person Added").format(person=new_name))
                    st.rerun()

        # ---- 分组管理 ----
        with st.expander(tr("Manage Groups"), expanded=False):
            group_create_col, _ = st.columns([2, 3])
            with group_create_col:
                new_group_name = st.text_input(
                    tr("Group Name"),
                    key="library_new_group_name",
                    max_chars=40,
                ).strip()
                if st.button(tr("Create Group"), key="library_create_group"):
                    if not new_group_name:
                        st.warning(tr("Please Enter Group Name"))
                    elif person_store.create_group(new_group_name):
                        st.success(tr("Group Created"))
                        st.rerun()
                    else:
                        st.warning(tr("Group Exists"))
            for g in person_store.list_groups():
                group_row_a, group_row_b, group_row_c = st.columns([3, 1, 4])
                with group_row_a:
                    st.markdown(f"**{g['name']}**")
                with group_row_b:
                    st.caption(f"{tr('Members')}: {g['count']}")
                with group_row_c:
                    if st.button(
                        tr("Delete Group"),
                        key=f"library_delete_group_{g['name']}",
                        width="content",
                    ):
                        person_store.delete_group(g["name"])
                        st.rerun()

    return chosen

def _render_base_script_picker(person: str) -> list[str]:
    """向导步骤②：展示所选人物的传记作为“基础脚本”，默认全选。

    勾中的文章即本次视频文案的唯一事实来源（全部取消则回退系统自动挖掘）。
    “自动全选”由每篇 checkbox 的 value=True 实现：换人时 key 带人名，新 key
    自然重新默认全选；同会话二次进入则保留用户已取消的选择。“全选/清空”
    通过 on_click 回调写 session_state，规避前端旧值覆盖。
    """
    if not person:
        return []
    try:
        from app.services import library, person_store
    except Exception as exc:  # pragma: no cover - 导入失败不应阻断页面
        st.caption(f"{tr('Local Library Unavailable')} ({type(exc).__name__})")
        return []
    if (
        not library.enabled()
        or library.db_path() is None
        or library.get_conn() is None
    ):
        return []

    profile = person_store.get_person(person)
    docs = (profile or {}).get("docs") or []
    valid_ids = [str(d.get("doc_id") or "") for d in docs if d.get("doc_id")]
    if not valid_ids:
        return []

    st.markdown(tr("Base Scripts"))
    st.caption(tr("Base Scripts Help"))

    def _set_all_base_scripts(value: bool):
        for doc_id in valid_ids:
            st.session_state[f"library_doc_pick_{person}_{doc_id}"] = value

    nav_all, nav_clear, _ = st.columns([1, 1, 3])
    nav_all.button(
        tr("Select All Base Scripts"),
        key=f"library_doc_pick_all_{person}",
        on_click=_set_all_base_scripts,
        args=(True,),
        width="stretch",
    )
    nav_clear.button(
        tr("Clear Base Scripts"),
        key=f"library_doc_pick_none_{person}",
        on_click=_set_all_base_scripts,
        args=(False,),
        width="stretch",
    )

    picked_docs: list[str] = []
    for d in docs:
        doc_id = str(d.get("doc_id") or "")
        if not doc_id:
            continue
        pick_key = f"library_doc_pick_{person}_{doc_id}"
        # 自动全选：新 key 首次出现时默认勾中；不再传 value，避免与
        # “全选/清空”回调直写 session_state 冲突产生默认值警告。
        st.session_state.setdefault(pick_key, True)
        pick_col, read_col = st.columns([3.4, 1.2], vertical_alignment="center")
        with pick_col:
            if st.checkbox(
                f"《{str(d.get('title') or '')[:60]}》 · "
                f"{d.get('category') or '—'} · "
                + tr("Article Chars").format(count=d.get("n_chars") or 0),
                key=pick_key,
            ):
                picked_docs.append(doc_id)
        with read_col:
            with st.expander(
                tr("Read Full Text"),
                key=f"library_doc_read_{person}_{doc_id}",
            ):
                body = ""
                try:
                    conn = library.get_conn()
                    if conn is not None:
                        row = conn.execute(
                            "SELECT body FROM documents WHERE doc_id = ?",
                            (doc_id,),
                        ).fetchone()
                        if row:
                            body = str(row["body"] or "")
                except Exception as exc:
                    st.error(f"Could not load article: {type(exc).__name__}")
                st.text(body[:4000] + ("…" if len(body) > 4000 else ""))

    st.info(
        tr("Base Scripts Count Selected").format(
            count=len(picked_docs), total=len(valid_ids)
        )
    )
    return picked_docs

def _open_material_settings_dialog():
    """供视频来源组件回调使用：直接打开素材服务设置。"""
    _open_settings_dialog("material")

def _render_brand(available_update: str | None = None):
    """渲染项目名称、当前版本和可选的更新入口。"""
    update_link = ""
    if available_update:
        update_label = html.escape(
            tr("Update Available").format(version=available_update)
        )
        # Streamlit 会继续用 Markdown 解析传入的 HTML。这里保持链接为单行，
        # 避免多行字符串的缩进被识别成代码块，导致页面直接显示 HTML 源码。
        update_link = (
            '<a class="mpt-brand__update" '
            f'href="{version_checker.LATEST_RELEASE_PAGE_URL}" '
            'target="_blank" rel="noopener noreferrer" '
            f'aria-label="{update_label}" title="{update_label}">'
            f"{update_label}</a>"
        )
    st.markdown(
        f"""
        <h1 class="mpt-brand">
            <span class="mpt-brand__name">MoneyPrinterTurbo</span>
            <a class="mpt-brand__version"
               href="https://github.com/harry0703/MoneyPrinterTurbo"
               target="_blank"
               rel="noopener noreferrer"
               aria-label="Open MoneyPrinterTurbo on GitHub"
               title="Open project on GitHub">v{html.escape(str(config.project_version))}</a>
            {update_link}
        </h1>
        """,
        unsafe_allow_html=True,
    )

@st.fragment(run_every="1s")
def _render_pending_version_check():
    """检查未完成时只刷新品牌区域，避免阻塞或反复执行整页表单。"""
    snapshot = version_checker.poll_available_update(config.project_version)
    if snapshot.complete:
        # 检查完成后刷新一次整页，让顶部栏改为静态渲染并停止 fragment 轮询。
        # 该刷新发生在后台请求完成之后，不会延迟初始页面的其它内容。
        st.rerun(scope="app")
    _render_brand()

def _render_top_bar():
    """渲染品牌、任务管理、设置和语言切换组成的页面顶部栏。"""
    # 顶部栏分为品牌区和操作区两个独立区域。窄屏下由 Streamlit
    # 将两个区域整体换行，操作区内部再根据剩余宽度自动换行。
    with st.container(key="top_bar"):
        # 管理页（素材库/设置）提供返回“工作台”的入口，避免用户被困在某个页。
        current_admin = st.session_state.get("admin_page")
        if current_admin and current_admin != "workspace":
            with st.container(key="top_bar_back"):
                if st.button(
                    tr("ws_menu_workspace"),
                    icon=":material/arrow_back:",
                    key="top_bar_back_generate",
                    width="stretch",
                    type="secondary",
                ):
                    # 直接把值写进 st.session_state["admin_page"] 会崩溃：
                    # 该键已被侧边栏 radio（key="admin_page"）实例化，
                    # Streamlit 禁止在 widget 实例化后改其绑定键 →
                    # StreamlitAPIException。改用中转键 admin_page_back，
                    # 交由 render_admin_sidebar() 在 radio 实例化前安全重置。
                    st.session_state["admin_page_back"] = "workspace"
                    st.rerun()
        brand_col, actions_col = st.columns(
            [3.5, 2.0],
            vertical_alignment="center",
            gap="small",
        )

    with brand_col:
        update_snapshot = version_checker.poll_available_update(config.project_version)
        if update_snapshot.complete:
            _render_brand(update_snapshot.available_version)
        else:
            _render_pending_version_check()

    with actions_col:
        with st.container(
            key="top_bar_actions",
            horizontal=True,
            horizontal_alignment="right",
            vertical_alignment="center",
            gap="small",
            width="stretch",
        ):
            _render_task_manager_entry()

            st.button(
                tr("Settings"),
                key="open_settings_dialog_button",
                type="secondary",
                icon=":material/settings:",
                width="content",
                on_click=_open_settings_dialog,
            )

            language_codes = list(locales.keys())
            selected_index = 0
            for i, code in enumerate(language_codes):
                if code == st.session_state.get("ui_language", ""):
                    selected_index = i

            selected_language_code = st.selectbox(
                "Language / 语言",
                options=language_codes,
                index=selected_index,
                format_func=lambda code: locales[code].get("Language", code),
                key="top_language_code_selector",
                label_visibility="collapsed",
                width=180,
            )
            if selected_language_code:
                previous_language = st.session_state.get("ui_language", "")
                if selected_language_code != previous_language:
                    logger.info(
                        "UI language changed by user: "
                        f"previous_language={previous_language or '<empty>'}, "
                        f"selected_language={selected_language_code}"
                    )
                    st.session_state["ui_language"] = selected_language_code
                    # 浏览器自动识别只影响当前会话；只有用户主动切换下拉框时才
                    # 写入 config.toml，后续新会话将优先使用该明确选择。
                    _set_runtime_config("ui", "language", selected_language_code)
                    _save_runtime_config()
                    # 切换语言后强制刷新，避免 selectbox 继续展示旧语言文案。
                    st.rerun()

def _render_generation_logs(task_id):
    """渲染后台任务日志快照，不从工作线程访问 Streamlit 会话状态。"""
    if config.ui.get("hide_log", False):
        return

    log_records = webui_task.get_task_logs(task_id)
    if not log_records:
        return

    st.code("\n".join(log_records))

def _render_generation_task_snapshot(task_id, task):
    """根据状态存储中的快照渲染进度、失败原因或最终成片。"""
    if not task:
        st.info(tr("Generating Video"))
        _render_generation_logs(task_id)
        return

    state = _normalize_task_state(task.get("state"))
    progress = max(0, min(100, int(task.get("progress", 0) or 0)))
    if state == const.TASK_STATE_PROCESSING:
        st.info(tr("Generating Video"))
        st.progress(
            progress,
            text=f"{tr('Task Progress')}: {progress}%",
        )
        _render_generation_logs(task_id)
        return

    if state == const.TASK_STATE_FAILED:
        error = str(task.get("error") or "").strip()
        message = tr("Video Generation Failed")
        st.error(f"{message}: {error}" if error else message)
        _render_generation_logs(task_id)
        return

    video_files = task.get("videos") or []
    if state != const.TASK_STATE_COMPLETE or not video_files:
        st.error(tr("Video Generation Failed"))
        _render_generation_logs(task_id)
        return

    st.success(tr("Video Generation Completed"))
    for warning in task.get("warnings") or []:
        if isinstance(warning, Mapping) and warning.get("code") == "batch_materials_reused":
            st.warning(
                tr("Batch Material Reuse Warning").format(
                    index=warning.get("video_index", ""), count=warning.get("count", 0)
                )
            )
        elif isinstance(warning, Mapping) and warning.get("code") == "sonilo_bgm_failed":
            st.warning(
                tr("Sonilo BGM Fallback Warning").format(
                    index=warning.get("video_index", "")
                )
            )
        elif (
            isinstance(warning, Mapping)
            and warning.get("code") == "elevenlabs_bgm_failed"
        ):
            st.warning(
                tr("ElevenLabs BGM Fallback Warning").format(
                    index=warning.get("video_index", "")
                )
            )
        else:
            st.warning(str(warning))

    try:
        player_cols = st.columns(len(video_files) * 2 + 1)
        for i, url in enumerate(video_files):
            with player_cols[i * 2 + 1]:
                st.video(url)
                if not os.path.isfile(url):
                    logger.warning(
                        f"generated video is unavailable for download: "
                        f"task_id={task_id}, video_file={url}"
                    )
                    continue

                download_label = tr("Download Video")
                if len(video_files) > 1:
                    download_label = f"{download_label} {i + 1}"
                download_name = _build_video_download_name(
                    task.get("video_subject"),
                    i + 1,
                    len(video_files),
                )
                with open(url, "rb") as video_file:
                    st.download_button(
                        download_label,
                        data=video_file,
                        file_name=download_name,
                        mime=mimetypes.guess_type(url)[0] or "video/mp4",
                        key=f"download_generated_video_{task_id}_{i}",
                        icon=":material/download:",
                        on_click="ignore",
                        width="stretch",
                    )
    except Exception as exc:
        logger.exception(
            f"failed to render generated video preview: task_id={task_id}, "
            f"video_files={video_files}, error={exc}"
        )

    _render_generation_logs(task_id)
    if st.session_state.get("handled_generation_task_id") != task_id:
        # Fragment 可能重复渲染同一个完成任务。无论是否开启自动打开目录,
        # 每个任务都只处理一次完成事件,避免重复弹出资源管理器或重复写入日志。
        st.session_state["handled_generation_task_id"] = task_id
        # 生成即入库:完成任务自动登记为一个 project(幂等),使"作品管理"
        # 不再是需要手动"从任务导入"的孤岛。失败只记录日志,不影响结果展示。
        try:
            from webui import projects as projects_service

            task_meta = dict(task or {})
            task_meta.setdefault("task_id", task_id)
            if video_files:
                task_meta.setdefault("video_file", video_files[0])
            projects_service._upsert_project_from_task(task_id, task_meta)
        except Exception as exc:
            logger.warning(
                f"failed to auto-register project for task {task_id}: "
                f"{type(exc).__name__}: {exc}"
            )
        if config.ui.get("open_task_folder_on_completion", True):
            open_task_folder(task_id)
        logger.info(f"{tr('Video Generation Completed')}: task_id={task_id}")

@st.fragment(run_every=webui_task.TASK_LOG_REFRESH_INTERVAL_SECONDS)
def _render_running_generation_task(task_id):
    """只在任务运行期间轮询；结束后切回静态结果，停止不必要的定时刷新。"""
    try:
        task = sm.state.get_task(task_id)
    except Exception as exc:
        logger.exception(
            f"failed to query WebUI generation task: task_id={task_id}, error={exc}"
        )
        st.error(tr("Video Generation Failed"))
        return

    state = _normalize_task_state((task or {}).get("state"))
    if state in {const.TASK_STATE_COMPLETE, const.TASK_STATE_FAILED}:
        _remove_active_generation_task(task_id)
        # 完整页面脚本现在没有耗时生成逻辑，可以安全 rerun 并把结果改为静态
        # 渲染。这样任务结束后不会让浏览器永久保留一个两秒轮询的 Fragment。
        st.rerun(scope="app")

    _render_generation_task_snapshot(task_id, task)

def _render_current_generation_task():
    """在生成按钮下方恢复当前页面最近提交任务的可查询 UI。"""
    task_id = st.session_state.get("current_generation_task_id", "")
    if not task_id:
        return

    try:
        task = sm.state.get_task(task_id)
    except Exception as exc:
        logger.exception(
            f"failed to query current WebUI task: task_id={task_id}, error={exc}"
        )
        st.error(tr("Video Generation Failed"))
        return

    state = _normalize_task_state((task or {}).get("state"))
    if state in {const.TASK_STATE_COMPLETE, const.TASK_STATE_FAILED}:
        _remove_active_generation_task(task_id)
        _render_generation_task_snapshot(task_id, task)
        return

    _render_running_generation_task(task_id)

def get_llm_provider_tips(provider_id, **kwargs):
    # LLM provider 说明文案统一使用 `llm_provider_tips.<provider_id>` 规则。
    # 这样新增 provider 时只需要在 locale 中补文案；没有文案时不展示提示块，
    # 避免 Main.py 里继续堆叠大量中英文硬编码说明。
    provider = get_llm_provider(provider_id)
    if provider is None:
        return ""

    # Provider 配置说明目前统一维护中文和英文两套规范模板；其它界面语言
    # 统一使用英文，避免在 locale 中复制英文后长期不同步。后续某个语种完成
    # 全量翻译后，再将它加入这里的独立维护范围。
    ui_language = st.session_state.get("ui_language", "en")
    tips_language = ui_language if ui_language in {"zh", "en"} else "en"
    tips = (
        locales.get(tips_language, {}).get("Translation", {}).get(provider.tips_key, "")
    )
    if not tips:
        return tips

    service_endpoint = provider.preferred_service_endpoint(
        prefer_international=tips_language == "en"
    )
    api_key_url = (
        service_endpoint.api_key_url
        if service_endpoint
        else provider.effective_api_key_url()
    )
    format_context = {
        "api_key_url": api_key_url,
        "default_model": provider.default_model,
        "default_base_url": (
            service_endpoint.base_url
            if service_endpoint
            else provider.effective_default_base_url
        ),
        "model_docs_url": (
            service_endpoint.model_docs_url
            if service_endpoint and service_endpoint.model_docs_url
            else provider.effective_model_docs_url(
                prefer_international=tips_language == "en"
            )
        ),
        **{
            f"default_{field.config_suffix}": field.default_value
            for field in provider.extra_fields
        },
        **kwargs,
    }
    try:
        return tips.format(**format_context)
    except Exception as e:
        logger.warning(f"format llm provider tips failed: {provider_id}, {e}")
        return tips

def format_llm_connection_error(provider_id, base_url, error):
    """为可明确定位的鉴权错误补充配置检查建议，同时保留原始响应。"""
    error_text = str(error or "").strip()
    normalized_error = error_text.lower()
    authentication_markers = (
        "401",
        "authentication",
        "invalid api key",
        "invalid_api_key",
        "unauthorized",
    )
    provider = get_llm_provider(provider_id)
    if provider is None or not provider.service_endpoints or not any(
        marker in normalized_error for marker in authentication_markers
    ):
        return error_text

    message = tr_optional(
        provider.authentication_error_key,
        fallback_language="en",
    )
    if not message:
        return error_text
    return message.format(base_url=base_url or "-", error=error_text)

def get_llm_provider_label(provider):
    return tr_optional(provider.label_key) or provider.default_label

def get_tts_provider_tips(provider_id):
    # TTS 配置说明与 LLM Provider 采用相同维护策略：只维护中英文，
    # 其它界面语言统一回退英文，避免复制后长期不同步。
    ui_language = st.session_state.get("ui_language", "en")
    tips_language = ui_language if ui_language in {"zh", "en"} else "en"
    return (
        locales.get(tips_language, {})
        .get("Translation", {})
        .get(f"tts_provider_tips.{provider_id}", "")
    )

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



support_locales = [
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
]
