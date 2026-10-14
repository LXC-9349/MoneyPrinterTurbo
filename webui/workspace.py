"""WebUI 项目工作区（以项目为中心的编辑 + 真分步向导）。

产品设计从"页面 + 表单"改为"项目工作区"：每个视频是一个项目（project），
用户在项目内用分步向导完成 内容 → 视觉 → 声音 → 字幕与生成。本模块负责两件事：

1. draft 数据层：把每个项目的编辑态（VideoParams）持久化到
   ``storage/projects/projects.json`` 的 ``draft`` 字段，编辑过程只作用于项目
   自己的 draft，不再实时写全局 ``config.app``，从根本上消除跨项目串味。
2. 工作区渲染：分步 stepper（只渲染当前步）+ 右侧粘性摘要栏 + 生成。

数据契约：一个 project 绑定一个 task（生成一次 = 一个 task_id）。draft 让项目
在"尚未生成"时也能存在并累积参数；打开工作区时若缺 draft，则从
``tasks/<task_id>/script.json`` 的 params 反推，老数据无需迁移。
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional
from uuid import uuid4

import streamlit as st
from loguru import logger

from app.models.schema import VideoParams
from app.utils import utils

# ── 分步向导步骤定义（顺序即进度条顺序）────────────────────────────────
# 每步是一个稳定业务 id；渲染与校验按 id 分派，避免把步序号写死在控件里。
STEP_CONTENT = "content"      # 内容：选人物 / 主题 + 文案
STEP_VISUAL = "visual"        # 视觉：素材源 + 画幅/分辨率/时长/镜头
STEP_AUDIO = "audio"          # 声音：配音 / 语速音量 / 背景音乐
STEP_OUTPUT = "output"        # 字幕与生成：字幕样式 + 摘要 + 生成/进度

WORKFLOW_STEPS: tuple[str, ...] = (STEP_CONTENT, STEP_VISUAL, STEP_AUDIO, STEP_OUTPUT)

# 会话内工作区状态键（与项目 draft 分离：会话键只管"当前打开哪个项目/哪一步"）
WS_PROJECT_KEY = "ws_project_id"
WS_STEP_KEY = "ws_step"


def _step_index(step: str) -> int:
    """步骤 id → 序号；未知 id 回退第一步。"""
    try:
        return WORKFLOW_STEPS.index(step)
    except ValueError:
        return 0


def ws_key(pid: str, field: str) -> str:
    """为某项目的某字段生成命名空间 widget key，避免跨项目/跨会话串味。"""
    return f"ws_{pid}_{field}"


# ── draft 数据层 ────────────────────────────────────────────────────────
def _projects_module():
    """惰性导入 projects，避免 projects↔workspace 的模块级循环依赖。"""
    from webui import projects  # noqa: WPS437
    return projects


def _params_to_draft(params: VideoParams) -> dict[str, Any]:
    """VideoParams → JSON 安全的 dict（用于持久化到 projects.json）。"""
    try:
        return params.model_dump(mode="json")
    except Exception as exc:  # pragma: no cover - 序列化兜底
        logger.warning(f"failed to serialize draft params: {exc}")
        return dict(params.__dict__)


def _derive_params_from_task(task_id: str) -> Optional[dict[str, Any]]:
    """从历史任务的 script.json 反推 params dict；无文件/异常返回 None。"""
    if not task_id:
        return None
    script_path = os.path.join(utils.task_dir(task_id), "script.json")
    if not os.path.isfile(script_path):
        return None
    try:
        with open(script_path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception as exc:  # pragma: no cover - 磁盘/JSON 异常降级
        logger.debug(f"failed to read task script for draft: task_id={task_id}, {exc}")
        return None
    params = data.get("params")
    return params if isinstance(params, dict) else None


def _default_params() -> VideoParams:
    """一个空白 draft：video_subject 为必填占位，其余走模型默认值。"""
    return VideoParams(video_subject="")


def load_draft(project_id: str) -> VideoParams:
    """读取项目 draft 为 VideoParams。

    优先级：projects.json 的 ``draft`` 字段 → 绑定 task 的 script.json.params
    → 空白默认。任何一步异常都安全降级，绝不因为坏 draft 让工作区崩页。
    """
    projects = _projects_module()
    entry = next(
        (p for p in projects._load_projects() if p.get("id") == project_id), None
    )
    raw: Optional[dict[str, Any]] = None
    if entry and isinstance(entry.get("draft"), dict):
        raw = entry["draft"]
    elif entry and entry.get("task_id"):
        raw = _derive_params_from_task(entry["task_id"])

    if raw:
        try:
            return VideoParams.model_validate(raw)
        except Exception as exc:  # pragma: no cover - 兼容脏 draft
            logger.warning(f"invalid draft for project {project_id}: {exc}")
    return _default_params()


def save_draft(
    project_id: str,
    params: VideoParams,
    *,
    step: Optional[str] = None,
    status: Optional[str] = None,
    title: Optional[str] = None,
) -> bool:
    """把 params 写回项目 draft（原子写盘）。可同时更新 step/status/title。

    返回是否成功找到并更新了项目条目。
    """
    projects = _projects_module()
    entries = projects._load_projects()
    entry = next((p for p in entries if p.get("id") == project_id), None)
    if entry is None:
        return False
    entry["draft"] = _params_to_draft(params)
    if step is not None:
        entry["step"] = step
    if status is not None:
        entry["status"] = status
    if title:
        entry["title"] = title[:200]
    from datetime import datetime

    entry["updated_at"] = datetime.now().timestamp()
    projects._save_projects(entries)
    return True


def create_project(mode: str, *, title: str = "") -> str:
    """新建一个草稿项目（尚无 task），返回 project_id。

    mode: "biography" | "normal"。draft 用空白 params 起，mode/person 落到条目
    上，供列表渲染与后续生成时读取。
    """
    projects = _projects_module()
    from datetime import datetime

    pid = f"p_{uuid4().hex[:12]}"
    now = datetime.now().timestamp()
    params = _default_params()
    if mode == "biography":
        params.video_source = "historical_archive"
    entry = {
        "id": pid,
        "task_id": "",
        "title": title or _default_title(mode),
        "mode": mode,
        "person": "",
        "language": "",
        "source": params.video_source,
        "status": "draft",
        "step": STEP_CONTENT,
        "created_at": now,
        "updated_at": now,
        "draft": _params_to_draft(params),
    }
    entries = projects._load_projects()
    entries.append(entry)
    projects._save_projects(entries)
    return pid


def _default_title(mode: str) -> str:
    return "传记项目" if mode == "biography" else "新建项目"


def get_project_entry(project_id: str) -> Optional[dict[str, Any]]:
    projects = _projects_module()
    return next(
        (p for p in projects._load_projects() if p.get("id") == project_id), None
    )


# ── 会话辅助：把"当前打开的项目 + 步骤"放进 session ──────────────────────
def open_workspace(project_id: str, step: Optional[str] = None) -> None:
    st.session_state[WS_PROJECT_KEY] = project_id
    st.session_state[WS_STEP_KEY] = step or STEP_CONTENT


def current_project_id() -> str:
    return str(st.session_state.get(WS_PROJECT_KEY) or "")


def current_step() -> str:
    step = str(st.session_state.get(WS_STEP_KEY) or STEP_CONTENT)
    return step if step in WORKFLOW_STEPS else STEP_CONTENT


def close_workspace() -> None:
    st.session_state.pop(WS_PROJECT_KEY, None)
    st.session_state.pop(WS_STEP_KEY, None)


# ── 工作区渲染（A3/A4）：项目列表 + 分步向导（只渲染当前步）───────────────
# 传记模式先行：素材源固定为历史档案、无跨步上传控件，适合作为垂直切片；
# 普通模式仍在一屏内组装参数，Phase C 再并入同一 stepper。
from webui import base  # base 不反向依赖 workspace，可安全置于模块顶层

# 打开工作区时把 draft 里“内容/来源/配音”等核心字段回填到运行期 config，使
# 既有面板控件（默认值来自 config.app）反映当前项目。此处只写内存态、不落盘，
# 全局 config.toml 仅在“设置”页保存，从根本上避免跨项目串味。
_DRAFT_APP_FIELDS = (
    "video_source",
    "video_subject",
    "video_script",
    "video_terms",
    "video_language",
    "biography_person",
    "voice_name",
    "bgm_file",
    "custom_system_prompt",
    "video_script_prompt",
)


def _t(key: str, **kwargs) -> str:
    text = base.tr(key)
    if kwargs:
        try:
            text = text.format(**kwargs)
        except Exception:  # pragma: no cover - 文案占位符不匹配时原样返回
            pass
    return text


def _apply_draft_to_runtime(params: VideoParams, mode: str) -> None:
    d = params.model_dump()
    for key in _DRAFT_APP_FIELDS:
        value = d.get(key)
        if isinstance(value, (str, int, float, bool)):
            base._set_runtime_config("app", key, value)
    if mode == "biography":
        base._set_runtime_config("app", "video_source", "historical_archive")
    if isinstance(d.get("biography_script"), dict):
        st.session_state["biography_script"] = d["biography_script"]


def _working_params(mode: str) -> VideoParams:
    """返回当前项目的可编辑 params，并缓存在会话中。

    真分步只渲染当前步，params 必须跨 rerun 累积，因此把它存在会话里；切换
    项目（ws_project_id 变化）时重新从 draft 载入并回填运行期 config。
    """
    pid = current_project_id()
    params = st.session_state.get("ws_params")
    if params is None or st.session_state.get("ws_params_pid") != pid:
        params = load_draft(pid)
        st.session_state["ws_params"] = params
        st.session_state["ws_params_pid"] = pid
        _apply_draft_to_runtime(params, mode)
    params.match_materials_to_script = bool(
        st.session_state.get("match_materials_to_script", False)
    )
    return params


def _step_key_label(step: str) -> str:
    return {
        STEP_CONTENT: _t("ws_step_content"),
        STEP_VISUAL: _t("ws_step_visual"),
        STEP_AUDIO: _t("ws_step_audio"),
        STEP_OUTPUT: _t("ws_step_output"),
    }.get(step, step)


def _goto_step(step: str) -> None:
    if step in WORKFLOW_STEPS:
        st.session_state[WS_STEP_KEY] = step


def render_workspace_root() -> bool:
    """工作区总入口：未打开项目→列表；已打开→分步编辑器。

    返回本次渲染是否提交了新生成任务（供上层跳过重复保存）。
    """
    pid = current_project_id()
    if not pid:
        _render_project_list()
        return False
    entry = get_project_entry(pid)
    if entry is None:
        close_workspace()
        _render_project_list()
        return False
    return _render_workspace(entry)


def _render_project_list() -> None:
    projects = _projects_module()
    entries = list(projects._load_projects())

    with st.container(key="workspace_list"):
        head = st.container()
        head.markdown(f"## 🎬 {_t('ws_menu_workspace')}")

        # 紧凑统计条：一眼看到项目总量与各状态分布（合并原 overview 指标）。
        stat_entries = list(entries)
        counts = {
            "draft": 0,
            "processing": 0,
            "done": 0,
            "failed": 0,
        }
        for e in stat_entries:
            counts[_normalize_status(e.get("status"))] = (
                counts.get(_normalize_status(e.get("status")), 0) + 1
            )
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric(f"📁 {_t('ws_metric_total')}", len(stat_entries))
        m2.metric(_t("ws_filter_draft"), counts.get("draft", 0))
        m3.metric(_t("ws_filter_processing"), counts.get("processing", 0))
        m4.metric(_t("ws_filter_done"), counts.get("done", 0))
        m5.metric(_t("ws_filter_failed"), counts.get("failed", 0))

        # 新建项目：先选类型再建，进入工作区第 1 步。
        bar = st.container()
        c_mode, c_new, c_search = bar.columns([2, 1, 3], vertical_alignment="center")
        # 直接预先把标签写入 options，不再用 format_func 映射：Streamlit AppTest
        # 会重复调用 format_func 来反推 index/value，与 locale 回退时容易触发
        # “value 不在 options 里”异常；生产 UI 上语义也完全一致。
        mode_options = {
            _t("ws_new_biography"): "biography",
            _t("ws_new_normal"): "normal",
        }
        mode_label_raw = c_mode.radio(
            _t("ws_new_project"),
            options=list(mode_options),
            horizontal=True,
            key="ws_new_mode",
            label_visibility="collapsed",
        )
        mode_label = mode_options.get(mode_label_raw, "biography")
        if c_new.button(
            _t("ws_new_project"),
            type="primary",
            icon=":material/add:",
            key="ws_create_project",
            width="stretch",
        ):
            pid = create_project(mode_label)
            open_workspace(pid, STEP_CONTENT)
            st.rerun()
            return
        keyword = c_search.text_input(
            _t("ws_search"),
            placeholder=_t("ws_search_ph"),
            key="ws_list_search",
            label_visibility="collapsed",
        ).strip().lower()

        # 状态筛选 chip。
        status_labels = {
            "all": _t("ws_filter_all"),
            "draft": _t("ws_filter_draft"),
            "processing": _t("ws_filter_processing"),
            "done": _t("ws_filter_done"),
            "failed": _t("ws_filter_failed"),
        }
        # 同 ws_new_mode：直接预格式化标签作为 options，避开 AppTest 对
        # format_func 的二次反查（locale 不同时会命中“value 不在 options”）。
        pick_label_to_key = {label: key for key, label in status_labels.items()}
        pick = st.segmented_control(
            _t("ws_filter_all"),
            options=list(status_labels.values()),
            default=status_labels["all"],
            selection_mode="single",
            key="ws_list_filter",
            label_visibility="collapsed",
        )
        chosen_filter = pick_label_to_key.get(pick) or "all"

        shown = [
            e
            for e in entries
            if chosen_filter == "all"
            or _normalize_status(e.get("status")) == chosen_filter
        ]
        if keyword:
            shown = [
                e
                for e in shown
                if keyword
                in str(e.get("title") or e.get("subject") or e.get("id") or "")
                .lower()
            ]
        shown.sort(key=lambda e: e.get("updated_at") or e.get("created_at") or 0, reverse=True)

        if not shown:
            st.info(_t("ws_list_empty"))
            return

        # 卡片网格：用 st.columns 每行固定 3 张，确定性多列布局。此前依赖 CSS
        # :has(> .st-key-ws_card_) 自适应网格在 Streamlit 容器 DOM 上匹配不到
        # （卡片不是纵向块的直接子节点），会退化成单列全宽、按钮被撑到巨大。
        # 窄屏由 styles.css 的 @media 让列堆叠回单列。
        per_row = 3
        with st.container(key="ws_card_grid"):
            for start in range(0, len(shown), per_row):
                cols = st.columns(per_row, gap="small")
                for col, e in zip(cols, shown[start:start + per_row]):
                    with col:
                        _render_project_card(e)


def _normalize_status(status: Optional[str]) -> str:
    status = (status or "draft").strip()
    return status if status in ("draft", "processing", "done", "failed") else "draft"


def _render_project_card(entry: dict) -> None:
    projects = _projects_module()
    pid = entry.get("id", "")
    title = entry.get("title") or entry.get("subject") or pid[:8]
    mode = entry.get("mode") or _projects_module()._project_mode(entry)
    status = _normalize_status(entry.get("status"))
    step = entry.get("step") or STEP_CONTENT
    with st.container(key=f"ws_card_{pid}", border=True):
        st.markdown(f"**{title}**")
        badge = {
            "draft": _t("ws_status_draft"),
            "processing": _t("ws_status_processing"),
            "done": _t("ws_status_done"),
            "failed": _t("ws_status_failed"),
        }[status]
        mode_label = _t("ws_new_biography") if mode == "biography" else _t("ws_new_normal")
        st.caption(f"{mode_label} · {badge} · {_step_key_label(step)}")
        ops = st.columns([2, 1])
        with ops[0]:
            if st.button(
                _t("ws_open"),
                key=f"ws_open_{pid}",
                type="primary",
                icon=":material/edit_note:",
                width="stretch",
            ):
                open_workspace(pid, entry.get("step") or STEP_CONTENT)
                st.rerun()
        with ops[1]:
            if st.button(
                "✕",
                key=f"ws_del_{pid}",
                icon=":material/delete:",
                width="stretch",
            ):
                _delete_project(pid)
                st.rerun()


def _delete_project(pid: str) -> None:
    projects = _projects_module()
    entries = projects._load_projects()
    remaining = [e for e in entries if e.get("id") != pid]
    projects._save_projects(remaining)


def _render_workspace(entry: dict) -> bool:
    mode = entry.get("mode") or "biography"
    params = _working_params(mode)

    _render_header(entry)
    _render_stepper()

    step = current_step()
    with st.container(key="ws_body"):
        left, right = st.columns([2.6, 1.0], vertical_alignment="top")
        with left:
            with st.container(key="ws_step_content"):
                # 传记与普通共用同一真分步 stepper（Phase C）：只渲染当前步，
                # 前进做校验门。低频项在各自面板内折叠进“高级选项”。
                submitted = _render_step(step, params, mode)
            _render_nav_buttons(step, params, mode)
        with right:
            with st.container(key="ws_rail"):
                _render_rail(params, mode)
    return submitted


def _render_header(entry: dict) -> None:
    pid = entry.get("id", "")
    projects = _projects_module()
    with st.container(key="ws_project_header"):
        hc = st.columns([5, 2], vertical_alignment="center")
        with hc[0]:
            new_title = st.text_input(
                _t("projects_rename_label"),
                value=entry.get("title", ""),
                key=f"ws_title_{pid}",
                label_visibility="collapsed",
            )
            if new_title and new_title != entry.get("title"):
                projects._rename_project(pid, new_title)
                entry["title"] = new_title
        with hc[1]:
            st.markdown(
                f"<div class='ws-status-badge'>{_t('ws_status_' + _normalize_status(entry.get('status')))}</div>",
                unsafe_allow_html=True,
            )
            if st.button(
                _t("ws_back_list"),
                key="ws_back_to_list",
                width="stretch",
            ):
                _commit_draft(entry.get("mode") or "biography")
                close_workspace()
                st.rerun()


def _render_stepper() -> None:
    max_idx = int(st.session_state.get("ws_max_step_idx", 0) or 0)
    step = current_step()
    idx = _step_index(step)
    if idx > max_idx:
        max_idx = idx
        st.session_state["ws_max_step_idx"] = max_idx
    cols = st.container(key="ws_stepper").columns(len(WORKFLOW_STEPS))
    for i, s in enumerate(WORKFLOW_STEPS):
        state = "current" if i == idx else ("done" if i < idx else "todo")
        label = f"{i + 1}. {_step_key_label(s)}"
        icon = "✓ " if state == "done" else ("● " if state == "current" else "○ ")
        with cols[i]:
            clickable = i <= max_idx
            if st.button(
                icon + label,
                key=f"ws_stepchip_{s}",
                type="primary" if state == "current" else "secondary",
                width="stretch",
                disabled=not clickable,
            ):
                _commit_draft((get_project_entry(current_project_id()) or {}).get("mode") or "biography")
                _goto_step(s)
                st.rerun()


def _commit_draft(mode: str) -> None:
    pid = current_project_id()
    params = st.session_state.get("ws_params")
    if not pid or not isinstance(params, VideoParams):
        return
    title = None
    entry = get_project_entry(pid)
    if entry:
        title = entry.get("title")
    save_draft(pid, params, step=current_step(), title=title)


def _render_nav_buttons(step: str, params: VideoParams, mode: str) -> None:
    idx = _step_index(step)
    nav = st.container()
    c_prev, _spacer, c_next = nav.columns([1, 3, 1], vertical_alignment="center")
    with c_prev:
        if idx > 0 and st.button(
            _t("ws_prev"),
            key="ws_prev",
            icon=":material/chevron_left:",
            width="stretch",
        ):
            _commit_draft(mode)
            _goto_step(WORKFLOW_STEPS[idx - 1])
            st.rerun()
    with c_next:
        if idx < len(WORKFLOW_STEPS) - 1 and st.button(
            _t("ws_next"),
            key="ws_next",
            type="primary",
            width="stretch",
        ):
            ok, msg = _validate_step(step, params, mode)
            if not ok:
                st.warning(msg)
                return
            _commit_draft(mode)
            _goto_step(WORKFLOW_STEPS[idx + 1])
            st.rerun()


def _validate_step(step: str, params: VideoParams, mode: str) -> tuple[bool, str]:
    if step == STEP_CONTENT:
        if mode == "biography":
            if not (params.biography_person or "").strip():
                return False, _t("ws_need_person")
        else:
            # 普通模式：主题与文案至少有一个，否则无法生成（与后端前置校验一致）。
            if not (params.video_subject or "").strip() and not (
                params.video_script or ""
            ).strip():
                return False, _t("ws_need_subject")
        return True, ""
    return True, ""


def _render_step(step: str, params: VideoParams, mode: str) -> bool:
    from webui import tasks, scripts, video, audio

    container = st.container()
    if step == STEP_CONTENT:
        if mode == "biography":
            chosen = tasks._render_person_picker(params.biography_person)
            if not chosen:
                chosen = str(
                    base.config.app.get("biography_person", "") or ""
                ).strip()
            params.biography_person = chosen
            base._set_runtime_config("app", "biography_person", chosen)
            if chosen:
                picked = tasks._render_base_script_picker(chosen)
                st.session_state[f"biography_picked_doc_ids_{chosen.strip()}"] = picked
                saved_script = st.session_state.get("biography_script")
                if (
                    isinstance(saved_script, dict)
                    and saved_script.get("person") == chosen
                    and saved_script.get("language", "") == (params.video_language or "")
                ):
                    params.biography_script = saved_script
        # 传记与普通都在内容步渲染文案面板：传记已选人物时面板会隐藏
        # 主题/关键词输入，普通模式则展示完整的主题/语言/段落数/文案输入。
        scripts._render_script_settings(container, params)
    elif step == STEP_VISUAL:
        # 传记固定历史档案（不给改来源的入口）；普通暴露“视频来源”选择。
        uploaded = video._render_video_settings(
            container, params, show_video_source=(mode != "biography")
        )
        st.session_state["ws_state_uploaded_files"] = list(uploaded or [])
    elif step == STEP_AUDIO:
        up_audio, up_bgm, voice_mode = audio._render_audio_settings(container, params)
        st.session_state["ws_state_uploaded_audio_file"] = up_audio
        st.session_state["ws_state_uploaded_bgm_file"] = up_bgm
        st.session_state["ws_state_voice_mode"] = voice_mode
    else:  # STEP_OUTPUT
        return _render_output_step(params)
    return False


def _render_output_step(params: VideoParams) -> bool:
    from webui import subtitles, generation

    subtitles._render_subtitle_settings(st.container(), params)
    uploaded_files = st.session_state.get("ws_state_uploaded_files", [])
    uploaded_audio_file = st.session_state.get("ws_state_uploaded_audio_file")
    uploaded_bgm_file = st.session_state.get("ws_state_uploaded_bgm_file")
    voice_mode = st.session_state.get("ws_state_voice_mode", base.VOICE_MODE_TTS)
    submitted = generation._render_generation_controls(
        params,
        uploaded_files,
        uploaded_audio_file,
        uploaded_bgm_file,
        voice_mode,
    )
    if submitted:
        _bind_generation_to_project(params)
    return bool(submitted)


def _bind_generation_to_project(params: VideoParams) -> None:
    pid = current_project_id()
    if not pid:
        return
    task_id = str(
        st.session_state.get("current_generation_task_id")
        or st.session_state.get("pending_generation_task_id")
        or ""
    )
    projects = _projects_module()
    entries = projects._load_projects()
    entry = next((p for p in entries if p.get("id") == pid), None)
    if entry is None:
        return
    if task_id:
        entry["task_id"] = task_id
    save_draft(pid, params, step=STEP_OUTPUT, status="processing")


def _render_rail(params: VideoParams, mode: str) -> None:
    st.markdown(f"**{_t('ws_rail_title')}**")
    if mode == "biography":
        person = (params.biography_person or "").strip()
        st.markdown(f"👤 **{person or '—'}**")
    else:
        st.markdown(f"🎯 **{params.video_subject or '—'}**")
    rows = [
        ("🌐", params.video_language or "—"),
        ("🎞️", params.video_source or "—"),
        ("🎙️", params.voice_name or "—"),
    ]
    for icon, value in rows:
        st.caption(f"{icon} {value}")
    missing = []
    if mode == "biography" and not (params.biography_person or "").strip():
        missing.append(_t("ws_step_content"))
    if (
        mode != "biography"
        and not (params.video_subject or "").strip()
        and not (params.video_script or "").strip()
    ):
        missing.append(_t("ws_step_content"))
    if missing:
        st.warning(_t("ws_next") + ": " + "、".join(missing))
