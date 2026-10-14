"""Project 工作区数据层（从 webui 下沉，无 streamlit 依赖）。

设计边界：`app/` 不能 import `webui/`（后者依赖 streamlit，会污染纯 ASGI 服
务），因此这里独立维护一份与 ``webui/projects.py`` + ``webui/workspace.py``
完全一致的读写实现。存储位置与数据契约保持不变——仍读写
``storage/projects/projects.json``，``draft`` 字段保存 ``VideoParams`` 的 JSON
快照，确保与既有 Streamlit 产生的历史数据互通、无需迁移。

一个 project 绑定一个 task（生成一次 = 一个 task_id）；``draft`` 让项目
在"尚未生成"时也能存在并累积编辑态。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime
from typing import Any, Optional
from uuid import uuid4

from loguru import logger

from app.models.schema import VideoParams
from app.utils import utils

# ── 分步向导步骤定义（与 webui/workspace.py 顺序一致，作为对外 API 契约）──
STEP_CONTENT = "content"  # 内容：选人物 / 主题 + 文案
STEP_VISUAL = "visual"    # 视觉：素材源 + 画幅/分辨率/时长/镜头
STEP_AUDIO = "audio"      # 声音：配音 / 语速音量 / 背景音乐
STEP_OUTPUT = "output"    # 字幕与生成：字幕样式 + 摘要 + 生成/进度

WORKFLOW_STEPS: tuple[str, ...] = (STEP_CONTENT, STEP_VISUAL, STEP_AUDIO, STEP_OUTPUT)

# 项目状态机（对外稳定值，沿用 webui 里的语义）。
PROJECT_STATUSES = ("draft", "processing", "done", "failed")


def _projects_dir() -> str:
    return os.path.join(utils.storage_dir(), "projects")


def _projects_file() -> str:
    pdir = _projects_dir()
    os.makedirs(pdir, exist_ok=True)
    return os.path.join(pdir, "projects.json")


def _write_json_atomic(target: str, payload: dict[str, Any]) -> None:
    pdir = os.path.dirname(target)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=pdir,
            prefix=f".{os.path.basename(target)}.",
            suffix=".tmp",
            delete=False,
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


def list_projects() -> list[dict[str, Any]]:
    """读取全部项目条目（保留已绑定 task 或已有 draft 的条目，与 webui 一致）。"""
    path = _projects_file()
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:  # pragma: no cover - 磁盘/JSON 异常降级
        logger.warning(f"failed to load projects: {exc}")
        return []
    projects = data.get("projects", [])
    if not isinstance(projects, list):
        return []
    return [
        p
        for p in projects
        if isinstance(p, dict) and (p.get("task_id") or p.get("draft"))
    ]


def _save_projects(projects: list[dict[str, Any]]) -> None:
    _write_json_atomic(_projects_file(), {"projects": projects})


def get_entry(project_id: str) -> Optional[dict[str, Any]]:
    return next((p for p in list_projects() if p.get("id") == project_id), None)


def normalize_status(status: Optional[str]) -> str:
    status = (status or "draft").strip()
    return status if status in PROJECT_STATUSES else "draft"


def project_mode(entry: dict[str, Any]) -> str:
    return entry.get("mode") or ("biography" if entry.get("person") else "normal")


def _params_to_draft(params: VideoParams) -> dict[str, Any]:
    try:
        return params.model_dump(mode="json")
    except Exception as exc:  # pragma: no cover - 序列化兜底
        logger.warning(f"failed to serialize draft params: {exc}")
        return dict(params.__dict__)


def _default_params() -> VideoParams:
    return VideoParams(video_subject="")


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


def load_draft(project_id: str) -> VideoParams:
    """读取项目 draft 为 VideoParams。

    优先级：projects.json 的 ``draft`` 字段 → 绑定 task 的 script.json.params
    → 空白默认。任何一步异常都安全降级，绝不因为坏 draft 让工作区崩页。
    """
    entry = get_entry(project_id)
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
    entries = list_projects()
    entry = next((p for p in entries if p.get("id") == project_id), None)
    if entry is None:
        return False
    entry["draft"] = _params_to_draft(params)
    if step is not None:
        entry["step"] = step
    if status is not None:
        entry["status"] = normalize_status(status)
    if title:
        entry["title"] = title[:200]
    entry["updated_at"] = datetime.now().timestamp()
    _save_projects(entries)
    return True


def create_project(mode: str, *, title: str = "") -> dict[str, Any]:
    """新建一个草稿项目（尚无 task），返回创建后的条目。

    mode: "biography" | "normal"。draft 用空白 params 起，mode/person 落到条目
    上，供列表渲染与后续生成时读取。传记模式把素材源固定为历史档案。
    """
    mode = mode if mode in ("biography", "normal") else "normal"
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
    entries = list_projects()
    entries.append(entry)
    _save_projects(entries)
    return entry


def _default_title(mode: str) -> str:
    return "传记项目" if mode == "biography" else "新建项目"


def rename_project(project_id: str, new_title: str) -> bool:
    """保存作品标题。仅改 projects 索引里的 title，不动任务目录。"""
    title = (new_title or "").strip()
    if not title:
        return False
    entries = list_projects()
    for p in entries:
        if p.get("id") == project_id:
            p["title"] = title[:200]
            p["updated_at"] = datetime.now().timestamp()
            _save_projects(entries)
            return True
    return False


def delete_project(project_id: str, *, purge_task_dir: bool = True) -> bool:
    """删除项目条目；若已绑定 task 且 ``purge_task_dir``，一并清理任务目录。

    与 webui 的 ``_delete_project_by_id`` 语义一致，但不依赖 streamlit/mpt_admin，
    这里直接对 storage/tasks/<task_id> 做安全删除（不创建目录）。
    """
    entries = list_projects()
    target = next((p for p in entries if p.get("id") == project_id), None)
    if not target:
        return False
    _save_projects([p for p in entries if p.get("id") != project_id])
    task_id = target.get("task_id", "")
    if purge_task_dir and task_id:
        tasks_root = utils.storage_dir("tasks", create=False)
        task_path = os.path.join(tasks_root, task_id)
        # 双保险：仅当解析后的路径仍在 tasks 根目录内才删除，避免异常 task_id 越界。
        real_root = os.path.abspath(tasks_root) + os.sep
        if (
            os.path.abspath(task_path).startswith(real_root)
            and os.path.isdir(task_path)
        ):
            try:
                shutil.rmtree(task_path, ignore_errors=True)
            except OSError as exc:  # pragma: no cover - 清理失败不阻断索引删除
                logger.warning(f"failed to delete task dir {task_path}: {exc}")
    return True


def update_entry(
    project_id: str,
    *,
    step: Optional[str] = None,
    status: Optional[str] = None,
    title: Optional[str] = None,
    person: Optional[str] = None,
    language: Optional[str] = None,
    source: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """更新条目的非 draft 元字段（供 PATCH 在没有完整 params 时使用）。"""
    entries = list_projects()
    entry = next((p for p in entries if p.get("id") == project_id), None)
    if entry is None:
        return None
    if step is not None and step in WORKFLOW_STEPS:
        entry["step"] = step
    if status is not None:
        entry["status"] = normalize_status(status)
    if title:
        entry["title"] = title[:200]
    if person is not None:
        entry["person"] = person
    if language is not None:
        entry["language"] = language
    if source is not None:
        entry["source"] = source
    entry["updated_at"] = datetime.now().timestamp()
    _save_projects(entries)
    return entry


def bind_task(project_id: str, task_id: str, *, status: str = "processing") -> bool:
    """把生成任务 id 绑定到项目条目（提交生成后调用）。"""
    entries = list_projects()
    entry = next((p for p in entries if p.get("id") == project_id), None)
    if entry is None:
        return False
    if task_id:
        entry["task_id"] = task_id
    entry["status"] = normalize_status(status)
    entry["updated_at"] = datetime.now().timestamp()
    _save_projects(entries)
    return True


def summary_projection(entry: dict[str, Any]) -> dict[str, Any]:
    """列表投影：去掉体积较大的 ``draft`` 全量快照，仅保留卡片所需字段。"""
    return {
        "id": entry.get("id", ""),
        "task_id": entry.get("task_id", ""),
        "title": entry.get("title", ""),
        "mode": project_mode(entry),
        "person": entry.get("person", ""),
        "language": entry.get("language", ""),
        "source": entry.get("source", ""),
        "status": normalize_status(entry.get("status")),
        "step": entry.get("step") or STEP_CONTENT,
        "created_at": entry.get("created_at"),
        "updated_at": entry.get("updated_at"),
        "has_draft": bool(entry.get("draft")),
    }
