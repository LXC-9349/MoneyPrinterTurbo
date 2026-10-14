"""Projects 作品管理页。

自包含渲染模块（与 mpt_admin.py 同模式，不 import webui.Main 避免循环依赖）。
复用 mpt_admin 的底层扫描函数 _task_rows（它们本身不依赖 Main.py）。

数据模型（方案 A：轻量索引，指向 task 目录）：
  storage/projects/projects.json
  { "projects": [ {id, task_id, title, mode, person, language, source, created_at, updated_at}, ... ] }
  一个 project 绑定一个 task（生成一次 = 一个项目）。
  - 列表 / 导入：从 storage/tasks 扫描已完成任务（_task_rows 已含状态/视频/大小）
  - 播放：st.video(final-*.mp4)
  - 重新生成：读取 script.json.params → 新 task_id → webui_task.submit_generation()
  - 导出：st.download_button 下载 final-*.mp4
  - 删除：移除 projects.json 条目 + sm.state.delete_task + shutil.rmtree(task_dir)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import webbrowser
from datetime import datetime
from typing import Any, Optional
from uuid import uuid4

import streamlit as st
from loguru import logger

from app.models import const
from app.models.schema import VideoParams
from app.services import state as sm
from app.services import webui_task
from app.utils import utils
from webui import mpt_admin

__all__ = ["render_projects_page"]


def t(key: str) -> str:
    """翻译:当前语言 → 英文 → 原始 key。

    语言键必须是 ``ui_language``(应用唯一写入的语言状态,见 base/mpt_admin)。
    按“当前语言优先、仅在该语言缺失时回退英文”的顺序查找;历史实现先把
    当前语言字典再被英文字典整体覆盖,导致中文用户也永远看到英文。
    """
    lang = (st.session_state.get("ui_language") or "").strip()
    for code in (lang, "en"):
        if not code:
            continue
        loc = (mpt_admin._LOCALES.get(code) or {}).get("Translation") or {}
        value = loc.get(key)
        if value is not None:
            return str(value)
    return key


def _projects_dir() -> str:
    return os.path.join(utils.storage_dir(), "projects")


def _projects_file() -> str:
    pdir = _projects_dir()
    os.makedirs(pdir, exist_ok=True)
    return os.path.join(pdir, "projects.json")


def _write_json_atomic(target: str, payload: dict[str, Any]) -> None:
    import tempfile

    pdir = os.path.dirname(target)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=pdir,
            prefix=f".{os.path.basename(target)}.", suffix=".tmp", delete=False,
        ) as f:
            tmp = f.name
            json.dump(payload, f, ensure_ascii=False, indent=4)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
        tmp = None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _load_projects() -> list[dict[str, Any]]:
    path = _projects_file()
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        logger.warning("failed to load projects: %s", exc)
        return []
    projects = data.get("projects", [])
    if not isinstance(projects, list):
        return []
    # 保留两类条目：已绑定 task 的正式作品（含历史数据），以及工作区新建、
    # 尚未生成但已有 draft 的草稿项目（draft 让项目能在“未生成”前累积参数）。
    return [
        p
        for p in projects
        if isinstance(p, dict) and (p.get("task_id") or p.get("draft"))
    ]


def _save_projects(projects: list[dict[str, Any]]) -> None:
    _write_json_atomic(_projects_file(), {"projects": projects})


def _upsert_project_from_task(task_id: str, meta: dict[str, Any], force_title: str = "") -> bool:
    """把 task 幂等地写入 projects 列表。返回是否新增。

    除身份字段外,尽量固化展示所需的快照(画幅/来源/时长/大小/封面),避免列表
    渲染时反复扫盘、也避免任务目录被清理后卡片变空。所有字段都取自 runnable
    的真实来源:``params``(script.json)与任务目录下的 final-*.mp4。
    """
    projects = _load_projects()
    existing = next((p for p in projects if p.get("task_id") == task_id), None)
    params = meta.get("params", {}) or {}
    # meta 可能没有 params(来自 mpt_admin._task_rows 的行)，补读 script.json。
    if not params:
        try:
            script_path = os.path.join(utils.task_dir(task_id), "script.json")
            if os.path.isfile(script_path):
                with open(script_path, "r", encoding="utf-8") as f:
                    params = (json.load(f) or {}).get("params", {}) or {}
        except Exception:
            params = {}

    snapshot = _project_snapshot(task_id, meta, params)

    if existing:
        # 补齐可能缺失的增强字段,不覆盖用户已编辑的 title。
        for k, v in snapshot.items():
            existing.setdefault(k, v)
        existing["updated_at"] = datetime.now().timestamp()
        _save_projects(projects)
        return False

    title = force_title or meta.get("subject") or params.get("video_subject") or task_id[:8] + "…"
    project = {
        "id": f"p_{uuid4().hex[:12]}",
        "task_id": task_id,
        "title": title,
        "mode": "biography" if params.get("biography_person") else "normal",
        "person": params.get("biography_person", ""),
        "language": params.get("video_language", ""),
        "source": params.get("video_source", ""),
        "created_at": datetime.now().timestamp(),
        "updated_at": datetime.now().timestamp(),
        **snapshot,
    }
    projects.append(project)
    _save_projects(projects)
    return True


def _project_snapshot(task_id: str, meta: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """采集一个项目卡片所需的客观元数据(缺失字段一律留空,不猜)。"""
    snapshot: dict[str, Any] = {
        "aspect": params.get("video_aspect", ""),
        "subject": params.get("video_subject", "") or meta.get("subject", ""),
        "tags": [],
    }
    mode = "biography" if params.get("biography_person") else "normal"
    snapshot["tags"] = [mode] + ([params.get("biography_person")] if params.get("biography_person") else [])

    video_path = meta.get("video_file") or ""
    if not video_path:
        video_path = _find_final_video(task_id)
    if video_path and os.path.isfile(video_path):
        snapshot["video_file"] = video_path
        try:
            snapshot["size_bytes"] = os.path.getsize(video_path)
        except OSError:
            pass
        snapshot["duration"] = _probe_duration(video_path)
        snapshot["thumbnail"] = _ensure_thumbnail(task_id, video_path)
    return snapshot


def _find_final_video(task_id: str) -> str:
    tdir = utils.task_dir(task_id)
    if not os.path.isdir(tdir):
        return ""
    candidates = sorted(
        f for f in os.listdir(tdir)
        if f.startswith("final-") and f.endswith(".mp4")
    )
    return os.path.join(tdir, candidates[0]) if candidates else ""


def _probe_duration(video_path: str) -> float:
    """用 ffprobe/ffmpeg 读取时长;失败返回 0(不阻塞)。"""
    try:
        ffprobe = shutil.which("ffprobe")
        if ffprobe:
            out = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", video_path],
                capture_output=True, text=True, timeout=30,
            ).stdout.strip()
            return round(float(out), 1) if out else 0
    except Exception:
        pass
    return 0


def _ensure_thumbnail(task_id: str, video_path: str) -> str:
    """抽首帧作为封面;已存在则直接复用。失败返回空字符串。"""
    thumb_dir = os.path.join(_projects_dir(), "thumbs")
    target = os.path.join(thumb_dir, f"{task_id}.jpg")
    if os.path.isfile(target) and os.path.getsize(target) > 0:
        return target
    try:
        os.makedirs(thumb_dir, exist_ok=True)
        ffmpeg = utils.get_ffmpeg_binary()
        subprocess.run(
            [ffmpeg, "-y", "-ss", "1", "-i", video_path, "-frames:v", "1",
             "-vf", "scale=480:-1", target],
            capture_output=True, text=True, timeout=60,
        )
        if os.path.isfile(target) and os.path.getsize(target) > 0:
            return target
    except Exception as exc:
        logger.warning("thumbnail extraction failed for %s: %s", task_id, exc)
    return ""


def _import_all_completed_tasks() -> int:
    """扫描已完成任务,幂等地全部写入 projects。返回新增数量。"""
    rows = mpt_admin._task_rows()
    task_ids = {p.get("task_id") for p in _load_projects()}
    added = 0
    for r in rows:
        if r.get("video_file") and r.get("task_id") not in task_ids:
            if _upsert_project_from_task(r["task_id"], r):
                added += 1
    return added


def _backfill_snapshot_fields() -> int:
    """为已入库条目补齐缺失的快照字段(封面/时长/大小/画幅/标签)。

    历史条目多为早期入库,只有身份字段;列表因此大片空图。这里就地补齐:
    - 对已有成片的条目重算 snapshot 并 setdefault 式合并(不覆盖用户已编辑的
      title,也不覆盖已存在的有效值)。
    - 任务目录已被清理的条目跳过(保持灰显,不臆造数据)。
    返回补齐的条目数。
    """
    projects = _load_projects()
    if not projects:
        return 0
    rows = mpt_admin._task_rows()
    row_by_id = {r.get("task_id"): r for r in rows}
    changed = 0
    for p in projects:
        tid = p.get("task_id", "")
        if not tid:
            continue
        meta = row_by_id.get(tid) or {}
        # 读取 params(优先运行时行,其次 script.json)。
        params = (meta.get("script") or {}).get("params") or {}
        if not params:
            params = _read_task_params(tid)
        try:
            snapshot = _project_snapshot(tid, meta, params)
        except Exception:
            continue
        touched = False
        for k, v in snapshot.items():
            if v in (None, "", 0, [], {}):
                continue
            if not p.get(k):
                p[k] = v
                touched = True
        if touched:
            p["updated_at"] = datetime.now().timestamp()
            changed += 1
    if changed:
        _save_projects(projects)
    return changed


def _read_task_params(task_id: str) -> dict[str, Any]:
    try:
        path = os.path.join(utils.task_dir(task_id), "script.json")
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return (json.load(f) or {}).get("params", {}) or {}
    except Exception:
        pass
    return {}


def _sync_projects() -> None:
    """项目页加载时的自愈:导入遗漏成片 + 补齐缺失字段(幂等、无副作用)。

    这是"数据管理"的生产级要求 —— 作品库应始终与磁盘上的成片一致,
    而不是依赖用户手动点"从任务导入"。
    额外补三份客观结论:早期任务在质量闸门/内容预检上线前产出,
    script.json 里没有质量/内容报告,卡片徽章恒为空;这里重算补齐(幂等、不覆盖)。
    """
    try:
        _import_all_completed_tasks()
        _backfill_snapshot_fields()
        _backfill_task_reports()
    except Exception as exc:  # pragma: no cover - 自愈失败不应阻断页面
        logger.warning("projects sync failed: %s", exc)


def _backfill_task_reports() -> int:
    """对已有成片但缺少质量/内容报告的任务,用**管线同款判定器**重算并写回。

    早期任务在质量闸门/内容预检上线前生成,script.json 里
    ``quality_report`` / ``content_report`` 为空,导致作品卡片的质量/内容
    徽章恒为空白——即便这些片已客观生成。此自愈只做两件事:

    - 质量:对真实成片跑 ``quality_gate.check_video_quality``(ffprobe 客观
      校验,零 LLM),按线上写回形状(列表 + video_path)。阈值读 ``config.app``
      的 ``quality_min_duration`` / ``quality_min_width``(0=不判定),与线上同。
    - 内容:对脚本文本跑 ``content_check.check_script``,目标时长复用线上
      ``task._content_target_seconds``(传记取传记目标,普通取 storyboard 秒数)。
    - 幂等且**只补不写**:仅在某字段缺失时写入;已存在的报告绝不覆盖。
      失败降级为警告,不阻断渲染。
    返回补齐了报告的条目数。
    """
    filled = 0
    try:
        tasks_root = utils.storage_dir("tasks", create=False)
    except Exception:  # pragma: no cover
        tasks_root = None
    if not tasks_root or not os.path.isdir(tasks_root):
        return 0
    try:
        from app.config import config as cfg
    except Exception:  # pragma: no cover
        cfg = None
    min_dur = 0.0
    min_w = 0
    if cfg is not None:
        try:
            min_dur = float(cfg.app.get("quality_min_duration", 0) or 0)
        except (TypeError, ValueError):
            min_dur = 0.0
        try:
            min_w = int(cfg.app.get("quality_min_width", 0) or 0)
        except (TypeError, ValueError):
            min_w = 0

    for tid in os.listdir(tasks_root):
        task_path = os.path.join(tasks_root, tid)
        if not os.path.isdir(task_path):
            continue
        video_file = mpt_admin._find_final_video(task_path)
        if not video_file or not os.path.isfile(video_file):
            continue
        script_file = os.path.join(task_path, "script.json")
        if not os.path.isfile(script_file):
            continue
        try:
            with open(script_file, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if not isinstance(payload, dict):
                continue
        except Exception:  # pragma: no cover
            continue
        params = payload.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        to_patch = {}

        # 质量报告:只补缺失;沿用线上形状(列表 + video_path)。
        quality_report = payload.get("quality_report")
        if not quality_report:
            try:
                from app.services import quality_gate

                qr = quality_gate.check_video_quality(
                    video_file,
                    requested_aspect=str(params.get("video_aspect") or ""),
                    min_duration=min_dur,
                    min_width=min_w,
                )
                qr["video_path"] = video_file
                to_patch["quality_report"] = [qr]
            except Exception:  # pragma: no cover - 探测失败不补,不臆造
                continue

        # 内容报告:只补缺失;目标时长复用线上 _content_target_seconds。
        content_report = payload.get("content_report")
        if not content_report:
            narration = str(payload.get("script") or payload.get("video_script") or "").strip()
            if narration:
                try:
                    from app.models.schema import VideoParams
                    from app.services import content_check, task

                    vp = VideoParams(**{k: v for k, v in params.items() if v is not None})
                    target = task._content_target_seconds(vp)
                    to_patch["content_report"] = content_check.check_script(
                        narration, target_seconds=target
                    )
                except Exception:  # pragma: no cover - 目标推导失败时不补
                    pass

        if not to_patch:
            continue
        try:
            from app.services import task_artifacts

            ok = task_artifacts.patch_script_data(tid, **to_patch)
            if ok:
                filled += len(to_patch)
        except Exception:  # pragma: no cover
            continue
    if filled:
        logger.info(f"backfilled objective reports for {filled} task fields")
    return filled


def _delete_project_by_id(project_id: str) -> bool:
    projects = _load_projects()
    target = next((p for p in projects if p.get("id") == project_id), None)
    if not target:
        return False
    task_id = target.get("task_id", "")
    _save_projects([p for p in projects if p.get("id") != project_id])
    try:
        mpt_admin._delete_task({"task_id": task_id, "task_path": utils.task_dir(task_id),
                                "state": const.TASK_STATE_FAILED})
    except Exception as exc:
        logger.warning("failed to delete task dir %s: %s", task_id, exc)
        shutil.rmtree(utils.task_dir(task_id), ignore_errors=True)
    return True


def _regenerate_project(project: dict[str, Any], rows: list[dict[str, Any]]) -> bool:
    task_id = project.get("task_id", "")
    row = next((r for r in rows if r.get("task_id") == task_id), None)
    script = (row or {}).get("script", {}) or {}
    params = script.get("params", {}) or {}
    if not params:
        logger.warning("no params for regeneration task %s", task_id)
        return False
    try:
        new_params = VideoParams(**{k: v for k, v in params.items() if v is not None})
    except Exception as exc:
        logger.warning("failed to build VideoParams for regeneration: %s", exc)
        return False
    new_task_id = str(uuid4())
    params["project_id"] = project.get("id", "")
    params["regenerated_from"] = task_id
    try:
        webui_task.submit_generation(task_id=new_task_id, params=new_params, capture_logs=True)
    except Exception as exc:
        logger.error("regeneration submit failed task_id=%s: %s", new_task_id, exc)
        return False
    projects = _load_projects()
    for p in projects:
        if p.get("id") == project.get("id", ""):
            p["task_id"] = new_task_id
            p["updated_at"] = datetime.now().timestamp()
            break
    _save_projects(projects)
    return True


def _mode_label(mode: str) -> str:
    return t("projects_mode_biography") if mode == "biography" else t("projects_mode_normal")


def _status_label(state_val: int, has_video: bool) -> str:
    if state_val == const.TASK_STATE_PROCESSING:
        return t("projects_status_running")
    if state_val == const.TASK_STATE_FAILED:
        return t("projects_status_failed")
    if state_val == const.TASK_STATE_COMPLETE or has_video:
        return t("projects_status_done")
    return t("projects_status_missing")


def _open_folder(path: str) -> None:
    if not os.path.isdir(path):
        return
    try:
        if os.name == "nt":
            os.startfile(path)
            return
        if sys.platform == "darwin":
            subprocess.Popen(["open", path], close_fds=True)
            return
        if os.environ.get("WSL_DISTRO_NAME"):
            subprocess.Popen(["explorer.exe", path], close_fds=True, stdout=subprocess.DEVNULL)
            return
        subprocess.Popen(["xdg-open", path], close_fds=True, stdout=subprocess.DEVNULL)
        return
    except Exception:
        pass
    try:
        webbrowser.open("file://" + os.path.abspath(path))
    except Exception:
        pass


def _params_or_none(v, alt: str = "") -> str:
    return str(v) if v else (alt or "—")


def _format_mtime(mtime: float) -> str:
    if not mtime:
        return ""
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")


def _render_projects_list(search: str) -> None:
    """渲染项目列表卡片。先读 projects 索引,再补运行/文件元数据。"""
    projects = _load_projects()
    rows = mpt_admin._task_rows()
    row_by_id = {r.get("task_id"): r for r in rows}
    row_by_id.update(_scan_completed_rows(projects, row_by_id))

    if not projects:
        st.info(t("projects_empty"))
        st.markdown(
            f"<div style='text-align:center;margin-top:12px;'>"
            f"<b>{t('projects_import_tasks')}</b> — 扫描已完成任务自动填入。</div>",
            unsafe_allow_html=True,
        )
        return

    def _sort_key(p):
        r = row_by_id.get(p.get("task_id"))
        return r.get("mtime", 0) if r else 0

    projects_sorted = sorted(projects, key=_sort_key, reverse=True)
    st.caption(
        f"<b>{t('projects_total').format(count=len(projects_sorted))}</b>",
        unsafe_allow_html=True,
    )

    # 筛选:关键词 + 模式 + 状态 + 排序。全部在内存中完成,不重扫磁盘。
    fc = st.columns(4)
    with fc[0]:
        q = (search or "").strip().lower()
    with fc[1]:
        mode_filter = st.selectbox(
            t("projects_filter_mode"),
            options=["all", "normal", "biography"],
            format_func=lambda v: {
                "all": t("projects_filter_all"),
                "normal": t("projects_mode_normal"),
                "biography": t("projects_mode_biography"),
            }[v],
            key="proj_filter_mode",
        )
    with fc[2]:
        status_filter = st.selectbox(
            t("projects_filter_status"),
            options=["all", "done", "running", "failed"],
            format_func=lambda v: {
                "all": t("projects_filter_all"),
                "done": t("projects_status_done"),
                "running": t("projects_status_running"),
                "failed": t("projects_status_failed"),
            }[v],
            key="proj_filter_status",
        )
    with fc[3]:
        sort_key = st.selectbox(
            t("projects_sort"),
            options=["time", "title", "duration", "size"],
            format_func=lambda v: {
                "time": t("projects_sort_time"),
                "title": t("projects_sort_title"),
                "duration": t("projects_sort_duration"),
                "size": t("projects_sort_size"),
            }[v],
            key="proj_sort",
        )

    def _row_state(p):
        r = row_by_id.get(p.get("task_id")) or {}
        has_video = bool(r.get("video_file")) or bool(p.get("video_file"))
        return _status_key(r.get("state", const.TASK_STATE_FAILED), has_video)

    filtered = projects_sorted
    if q:
        filtered = [
            p for p in filtered
            if q in " ".join([
                p.get("title", ""),
                p.get("person", ""),
                p.get("subject", ""),
                (row_by_id.get(p.get("task_id")) or {}).get("subject", ""),
            ]).lower()
        ]
    if mode_filter != "all":
        filtered = [p for p in filtered if _project_mode(p) == mode_filter]
    if status_filter != "all":
        filtered = [p for p in filtered if _row_state(p) == status_filter]

    if not filtered:
        st.info(t("projects_empty"))
        return

    filtered = _sort_projects(filtered, row_by_id, sort_key)

    # ---- 批量操作:选择 + 批量删除 ----
    with st.container(key="proj_batch_bar"):
        b1, b2, b3 = st.columns([3, 1, 1], vertical_alignment="center")
        with b1:
            batch_ids = st.multiselect(
                t("projects_select"),
                options=[p.get("id") for p in filtered if p.get("id")],
                format_func=lambda pid: next(
                    (
                        (p.get("title") or p.get("subject") or pid)
                        for p in filtered
                        if p.get("id") == pid
                    ),
                    pid,
                ),
                key="proj_batch_select",
            )
        with b2:
            if st.button(
                t("projects_batch_delete"),
                key="proj_batch_delete",
                icon=":material/delete_sweep:",
                width="stretch",
                disabled=not batch_ids,
            ):
                n = _batch_delete_projects(batch_ids)
                if n:
                    st.success(t("projects_batch_deleted").format(count=n))
                else:
                    st.error(t("projects_delete_failed"))
                st.session_state.pop("proj_batch_select", None)
                st.rerun()
                return
        with b3:
            st.caption(
                t("projects_selected").format(count=len(batch_ids))
                if batch_ids
                else ""
            )

    cols = st.columns(3)
    for i, p in enumerate(filtered):
        r = row_by_id.get(p.get("task_id")) or {}
        pid = p.get("id", "")
        task_id = p.get("task_id", "")
        title = p.get("title") or r.get("subject") or p.get("subject") or task_id[:8] + "..."
        mode = _project_mode(p)
        status = _status_label(
            r.get("state", const.TASK_STATE_FAILED),
            bool(r.get("video_file")) or bool(p.get("video_file")),
        )
        thumb = p.get("thumbnail", "")
        duration = p.get("duration") or r.get("duration") or 0
        size_bytes = p.get("size_bytes") or r.get("size_bytes") or 0
        with cols[i % 3]:
            with st.container(key=f"proj_card_{pid}"):
                if thumb and os.path.isfile(thumb):
                    st.image(thumb, width="stretch")
                st.markdown(f"**{title}**")
                bits = [
                    f"{_mode_label(mode)}",
                    f"{status}",
                    f"{_format_mtime(r.get('mtime', 0))}",
                ]
                if duration:
                    bits.append(f"{duration:.0f}s")
                if size_bytes:
                    bits.append(f"{size_bytes/1024/1024:.1f}MB")
                st.caption("  |  ".join(bits))
                # 质量徽章:成片是否通过客观校验(时长/流/分辨率/黑帧)。
                q_badge = _project_quality_badge(p, r)
                if q_badge:
                    q_pass = q_badge == t("projects_quality_ok")
                    st.markdown(
                        f"<span class='{_quality_cls(q_pass)}'>{q_badge}</span>",
                        unsafe_allow_html=True,
                    )
                c_badge = _project_content_badge(p, r)
                if c_badge:
                    c_pass = c_badge == t("projects_content_ok")
                    st.markdown(
                        f"<span class='{_quality_cls(c_pass)}'>{c_badge}</span>",
                        unsafe_allow_html=True,
                    )
                # 质量/内容未过时,显式提示"建议重新生成"(重生成按钮就在下方)。
                q_fail = bool(q_badge) and q_badge != t("projects_quality_ok")
                c_fail = bool(c_badge) and c_badge != t("projects_content_ok")
                if q_fail or c_fail:
                    st.caption("⚠ " + t("projects_regen_suggest"))
                op_cols = st.columns(3)
                with op_cols[0]:
                    if st.button(t("projects_play"), key=f"proj_card_play_{pid}",
                                 icon=":material/play_arrow:"):
                        st.session_state["project_detail_id"] = pid
                        st.rerun()
                        return
                with op_cols[1]:
                    if st.button(t("projects_regenerate"), key=f"proj_card_regen_{pid}",
                                 icon=":material/replay:"):
                        if _regenerate_project(p, rows):
                            st.success(t("projects_regen_done"))
                        else:
                            st.error(t("projects_regen_error"))
                        st.rerun()
                        return
                with op_cols[2]:
                    if st.button(t("projects_delete"), key=f"proj_card_del_{pid}",
                                 icon=":material/delete:"):
                        st.session_state["project_del_confirm"] = pid
                        st.rerun()
                        return


def _scan_completed_rows(projects, row_by_id) -> dict[str, Any]:
    """为一个 project 补读 script.json / 任务目录元数据(仅当 _task_rows 缺失时)。"""
    extra: dict[str, Any] = {}
    for p in projects:
        tid = p.get("task_id")
        if not tid or tid in row_by_id:
            continue
        tdir = utils.task_dir(tid)
        script_path = os.path.join(tdir, "script.json")
        meta: dict[str, Any] = {"task_id": tid}
        if os.path.isfile(script_path):
            try:
                with open(script_path, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
                meta["script"] = data
                meta["subject"] = (data.get("params") or {}).get("video_subject", "")
                meta["params"] = data.get("params") or {}
            except Exception:
                pass
        video = _find_final_video(tid)
        if video:
            meta["video_file"] = video
            try:
                meta["mtime"] = os.path.getmtime(video)
            except OSError:
                pass
        extra[tid] = meta
    return extra


def _project_mode(p: dict[str, Any]) -> str:
    return p.get("mode") or ("biography" if p.get("person") else "normal")


def _project_quality_badge(p: dict[str, Any], r: dict[str, Any]) -> str:
    """从 script.json 的 quality_report 派生质量徽章文案(空串=无报告)。

    质量是产品一等公民:列表上也应一眼看到成片是否通过客观校验(时长/流/
    分辨率/黑帧),而不是只在日志里。返回纯文本,由调用方着色。
    """
    script = (r.get("script") or {}) or {}
    reports = script.get("quality_report")
    if not reports:
        return ""
    if isinstance(reports, dict):
        reports = [reports]
    if not isinstance(reports, list) or not reports:
        return ""
    passed = all(bool(rep.get("passed")) for rep in reports if isinstance(rep, dict))
    issues = []
    for rep in reports:
        if isinstance(rep, dict):
            issues.extend(rep.get("issues") or [])
    if passed:
        return t("projects_quality_ok")
    if issues:
        return t("projects_quality_warn").format(count=len(issues))
    return t("projects_quality_warn").format(count=1)


def _quality_cls(passed: bool) -> str:
    return "mpt-badge-ok" if passed else "mpt-badge-err"


def _rename_project(project_id: str, new_title: str) -> bool:
    """保存作品标题。仅改 projects 索引里的 title,不动任务目录。"""
    title = (new_title or "").strip()
    if not title:
        return False
    projects = _load_projects()
    for p in projects:
        if p.get("id") == project_id:
            p["title"] = title[:200]
            p["updated_at"] = datetime.now().timestamp()
            _save_projects(projects)
            return True
    return False


def _set_project_tags(project_id: str, tags: list[str]) -> bool:
    """保存作品标签(用户自定义覆盖自动派生值)。"""
    projects = _load_projects()
    for p in projects:
        if p.get("id") == project_id:
            p["tags"] = [str(x)[:40] for x in tags][:20]
            p["updated_at"] = datetime.now().timestamp()
            _save_projects(projects)
            return True
    return False


def _sort_projects(items: list[dict[str, Any]], row_by_id: dict[str, Any], key: str):
    """按选择的键排序(时间/标题/时长/大小)。"""
    if key == "title":
        return sorted(
            items,
            key=lambda p: (p.get("title") or p.get("subject") or "").lower(),
        )
    if key in ("duration", "size"):
        field = "duration" if key == "duration" else "size_bytes"

        def _m(p):
            r = row_by_id.get(p.get("task_id")) or {}
            return p.get(field) or r.get(field) or 0

        return sorted(items, key=_m, reverse=True)
    # 默认:按 mtime 倒序(最新在前)
    return sorted(
        items,
        key=lambda p: (row_by_id.get(p.get("task_id")) or {}).get("mtime", 0),
        reverse=True,
    )


def _batch_delete_projects(project_ids: list[str]) -> int:
    """批量删除作品(含任务目录)。返回成功数。"""
    count = 0
    for pid in project_ids:
        if _delete_project_by_id(pid):
            count += 1
    return count


def _project_content_badge(p: dict[str, Any], r: dict[str, Any]) -> str:
    """从 script.json 的 content_report 派生内容质量徽章(空串=无报告)。"""
    scripts = (r.get("script") or {}) or {}
    report = scripts.get("content_report")
    if not report:
        return ""
    if isinstance(report, list):
        report = report[0] if report else {}
    if not isinstance(report, dict):
        return ""
    if report.get("passed"):
        return t("projects_content_ok")
    n = len(report.get("issues") or []) or 1
    return t("projects_content_warn").format(count=n)


def _status_key(state_val: int, has_video: bool) -> str:
    if state_val == const.TASK_STATE_PROCESSING:
        return "running"
    if state_val == const.TASK_STATE_FAILED and not has_video:
        return "failed"
    if state_val == const.TASK_STATE_COMPLETE or has_video:
        return "done"
    return "failed"


def _render_project_detail(detail_id: str) -> None:
    """渲染单个项目详情（播放 + 参数 + 5 项操作）。"""
    projects = _load_projects()
    project = next((p for p in projects if p.get("id") == detail_id), None)
    if not project:
        st.info(t("projects_detail"))
        return
    rows = mpt_admin._task_rows()
    r = next((x for x in rows if x.get("task_id") == project.get("task_id")), {})
    task_id = project.get("task_id", "")
    title = project.get("title") or r.get("subject") or task_id[:8] + "…"
    safe = "".join(ch if ch.isalnum() else "_" for ch in detail_id)[:40]

    st.header(title)

    # 重命名(文案已承诺,此前缺失):标题可编辑,只改索引不动任务目录。
    with st.expander(t("projects_rename"), expanded=False):
        new_title = st.text_input(
            t("projects_rename_label"),
            value=title,
            key=f"proj_rename_input_{safe}",
        )
        if st.button(
            t("projects_rename_save"),
            key=f"proj_rename_save_{safe}",
            icon=":material/edit:",
            disabled=not new_title.strip(),
        ):
            if _rename_project(detail_id, new_title):
                st.success(t("projects_rename_done"))
                st.rerun()
            else:
                st.error(t("projects_rename_failed"))

    # 标签编辑:tags 默认自动派生(mode+person),用户可覆盖为自定义标签。
    with st.expander(t("projects_tags"), expanded=False):
        cur_tags = ", ".join(project.get("tags") or [])
        tags_input = st.text_input(
            t("projects_tags_label"),
            value=cur_tags,
            key=f"proj_tags_input_{safe}",
            help=t("projects_tags_help"),
        )
        if st.button(
            t("projects_tags_save"),
            key=f"proj_tags_save_{safe}",
            icon=":material/label:",
        ):
            new_tags = [s.strip() for s in tags_input.split(",") if s.strip()]
            if _set_project_tags(detail_id, new_tags):
                st.success(t("projects_tags_done"))
                st.rerun()
            else:
                st.error(t("projects_rename_failed"))

    # 播放区
    video = r.get("video_file", "")
    if os.path.isfile(video):
        st.video(video)
    elif r.get("state") == const.TASK_STATE_PROCESSING:
        st.warning(t("projects_status_running"))
    else:
        st.info(t("projects_export_missing"))

    # 参数快照
    params = r.get("script", {}).get("params", {}) or {}
    if params:
        st.divider()
        st.markdown(t("projects_params_snapshot"))
        c1, c2 = st.columns(2)
        with c1:
            st.markdown(f"**{t('projects_person')}**: {_params_or_none(params.get('biography_person'), r.get('subject'))}")
            st.markdown(f"**{t('projects_subject')}**: {params.get('video_subject') or '—'}")
        with c2:
            st.markdown(f"**{t('projects_language')}**: {params.get('video_language') or '—'}")
            st.markdown(f"**{t('projects_source')}**: {params.get('video_source') or '—'}")
            st.markdown(f"**{t('projects_aspect')}**: {params.get('video_aspect') or '—'}")
            st.markdown(f"**{t('projects_mode')}**: {t('projects_mode_biography') if params.get('biography_person') else t('projects_mode_normal')}")

    # 文案
    script_text = (r.get("script") or {}).get("script") or (r.get("script") or {}).get("biography_script") or ""
    if script_text:
        st.divider()
        st.markdown(t("projects_script"))
        with st.expander(t("projects_script"), expanded=True):
            st.text(str(script_text)[:6000])

    st.divider()
    op_cols = st.columns(5)
    with op_cols[0]:
        if st.button(t("projects_regenerate"), icon=":material/replay:", key=f"proj_det_regen_{detail_id}"):
            if _regenerate_project(project, rows):
                st.success(t("projects_regen_done"))
            else:
                st.error(t("projects_regen_error"))
            st.rerun()
            return
    with op_cols[1]:
        if os.path.isfile(video):
            with open(video, "rb") as f:
                vid_bytes = f.read()
            st.download_button(
                t("projects_export"), icon=":material/download:", key=f"proj_det_export_{detail_id}",
                data=vid_bytes, file_name=os.path.basename(video), mime="video/mp4",
            )
        else:
            st.caption(t("projects_export_missing"))
    with op_cols[2]:
        if st.button(t("projects_open_dir"), icon=":material/folder_open:", key=f"proj_det_open_{detail_id}"):
            _open_folder(utils.task_dir(task_id))
            st.rerun()
            return
    with op_cols[3]:
        if st.button(t("projects_delete"), icon=":material/delete:", key=f"proj_det_del_{detail_id}"):
            st.session_state["project_del_confirm"] = detail_id
            st.rerun()
            return
    with op_cols[4]:
        if st.button(t("projects_back"), icon=":material/arrow_back:", key=f"proj_det_back_{detail_id}"):
            st.session_state["project_detail_id"] = None
            st.rerun()
            return


def _render_delete_confirm(confirm_id: str) -> None:
    """删除确认对话框（列表与详情共用）。"""
    projects = _load_projects()
    project = next((p for p in projects if p.get("id") == confirm_id), None)
    title = (project or {}).get("title") or confirm_id
    with st.container(key="proj_del_dialog"):
        st.warning(f"{t('projects_delete_confirm').format(title=title)}")
        dc = st.columns(2)
        with dc[0]:
            if st.button(t("projects_delete"), key=f"proj_del_confirm_yes_{confirm_id}",
                         icon=":material/delete:"):
                if _delete_project_by_id(confirm_id):
                    st.success(t("projects_deleted"))
                else:
                    st.error(t("projects_delete_failed"))
                st.session_state["project_detail_id"] = None
                st.session_state["project_del_confirm"] = None
                st.rerun()
                return
        with dc[1]:
            if st.button(t("projects_back"), key=f"proj_del_confirm_no_{confirm_id}",
                         icon=":material/close:"):
                st.session_state["project_del_confirm"] = None
                st.rerun()
                return


def render_projects_page() -> None:
    # 数据管理自愈:先与磁盘上的成片同步(导入遗漏 + 补齐字段),再渲染。
    _sync_projects()

    # 横幅
    with st.container(key="projects_page_banner"):
        st.markdown("## 📁 " + t("projects_menu"))
        st.caption(t("projects_hint"))

    # 顶部操作：搜索 + 导入 + 返回生成
    ac = st.columns(4)
    with ac[0]:
        search = st.text_input(t("projects_search"), placeholder=t("projects_search_ph"),
                               key="proj_search")
    with ac[1]:
        if st.button(t("projects_import_tasks"), icon=":material/import_export:", key="proj_import"):
            n = _import_all_completed_tasks()
            st.success(t("projects_imported").format(count=n))
            st.rerun()
            return
    with ac[2]:
        if st.button(t("projects_back"), icon=":material/arrow_back:", key="proj_back",
                     width="content"):
            st.session_state["admin_page_back"] = "generate"
            st.rerun()
            return

    # 删除确认（出现在列表或详情之上）
    confirm = st.session_state.get("project_del_confirm")
    if confirm:
        _render_delete_confirm(confirm)
        return

    # 详情页
    detail_id = st.session_state.get("project_detail_id")
    if detail_id:
        _render_project_detail(detail_id)
        return

    # 列表
    st.divider()
    _render_projects_list(search)
