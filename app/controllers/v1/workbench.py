"""以“人物 → 任务”为中心的工作台后端（里程碑 1）。

提供（均挂在独立子前缀 `/api/v1/workbench` 下，避开遗留 `/tasks`、`/tasks/{task_id}` 实时查询接口）：
* GET /persons/dashboard —— 人物管理页一次返回：人物网格（含每人的视频/文章/进行中
  任务/最近更新聚合统计）+ 候选（史料库尚未使用的人物）+ 排序/筛选选项。统计来自
  task_repo.counts_by_person() 的一次 GROUP BY，绝不做逐人查询（N+1）。
* GET /persons/suggest —— 新建人物时按姓名联想史料库候选（惰性物化前的检索）。
* GET /tasks —— 任务列表（筛选 / 搜索），来自 tasks 表。
* GET /tasks/active —— 顶栏“进行中任务”抽屉：进行中/排队任务，附带从实时任务状态
  合并回来的阶段与进度，并把新进度回写任务表（懒同步，不侵入生成链路）。

只读为主，写入集中在 tasks 观测表；不影响既有生成 / 人物叠加层链路。
"""

import threading
from datetime import datetime, timezone

from fastapi import Depends, Query, Request

from app.controllers import base
from app.controllers.v1.base import new_router
from app.models.schema import BaseResponse
from app.services import person_store, state as sm, task_repo
from app.utils import utils

router = new_router(dependencies=[Depends(base.verify_token)])
# 独立子前缀：遗留 video.py 已占用 /api/v1/tasks 与 /api/v1/tasks/{task_id}（实时
# 内存态查询），工作台的任务观测接口若同名会被先注册者遮蔽 / 误匹配为 404。
router.prefix = "/api/v1/workbench"

# projects.json → tasks 的一次性迁移：首次访问工作台时惰性执行，幂等，仅一次。
_migrated = False
_migrate_lock = threading.Lock()


def _ensure_projects_migrated() -> None:
    global _migrated
    if _migrated:
        return
    with _migrate_lock:
        if _migrated:
            return
        try:
            task_repo.migrate_projects()
        except Exception:  # pragma: no cover - 迁移失败不阻断页面，下次再试
            pass
        _migrated = True


_DEFAULT_ORDER = [
    "pinned",
    "active",
    "recent_generated",
    "recent_edited",
    "name",
]
_SORT_KEYS = {"recent_generated", "video_count", "article_count", "name", "dynasty"}


def _decorate(person: dict, stats: dict) -> dict:
    name = person.get("person", "")
    s = stats.get(name, {})
    return {
        **person,
        "video_count": s.get("video_count", 0),
        "article_count": s.get("article_count", 0),
        "article_enabled_count": s.get("article_enabled_count", 0),
        "active_task_count": s.get("active_task_count", 0),
        "last_updated": s.get("last_updated"),
        "used": (
            s.get("video_count", 0) > 0
            or s.get("article_count", 0) > 0
            or s.get("active_task_count", 0) > 0
            or person.get("source") == "custom"
            or person.get("pinned")
        ),
    }


def _epoch(item: dict) -> float:
    """把任务表/文章推导的 ISO8601 Z 时间转成 epoch，缺失归 0（降序时排末尾）。"""
    v = item.get("last_updated")
    if not v:
        return 0.0
    try:
        return datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _sort_key(item: dict, sort: str):
    if sort == "video_count":
        return (-item["video_count"], item["person"])
    if sort == "article_count":
        return (-item["article_count"], item["person"])
    if sort == "name":
        return (item["person"],)
    if sort == "dynasty":
        return (item.get("dynasty") or "zzz", item["person"])
    if sort == "recent_generated":
        return (-_epoch(item), item["person"])
    # 默认序：①置顶 ②有进行中 ③有成片/使用过优先 ④最近时间降序 ⑤名称。
    return (
        not item.get("pinned"),
        item.get("active_task_count", 0) == 0,
        -(1 if item.get("used") else 0),
        -_epoch(item),
        item["person"],
    )


@router.get("/persons/dashboard", response_model=BaseResponse, summary="Person workbench grid + stats")
def persons_dashboard(
    request: Request,
    keyword: str = Query("", description="姓名/别名/拼音关键词"),
    dynasty: str = Query("", description="朝代筛选"),
    category: str = Query("", description="类别筛选"),
    group: str = Query("", description="分组筛选"),
    status: str = Query("", description="有进行中/有成片/无记录/已归档：active|videos|none|archived"),
    sort: str = Query("", description="排序：recent_generated|video_count|article_count|name|dynasty，留空为默认序"),
):
    _ensure_projects_migrated()
    include_hidden = status == "archived"
    raw = person_store.list_persons(
        dynasty=dynasty or "",
        category=category or "",
        group=group or "",
        keyword=keyword or "",
        include_hidden=include_hidden,
    )
    stats = task_repo.counts_by_person()
    items = [_decorate(p, stats) for p in raw]

    if status == "active":
        items = [p for p in items if p["active_task_count"] > 0]
    elif status == "videos":
        items = [p for p in items if p["video_count"] > 0]
    elif status == "none":
        items = [p for p in items if not p["used"]]
    elif status == "archived":
        items = [p for p in items if p.get("hidden")]

    grid = sorted(items, key=lambda x: _sort_key(x, sort if sort in _SORT_KEYS else ""))
    # 候选：史料库里、有语料但“从未使用”的人物——无任何任务/文章历史（含失败任务），
    # 且非自建/未置顶。用 stats 的键集排除有活动记录的人物，避免失败任务的人物仍当候选。
    used_names = set(stats)
    candidates = sorted(
        (
            p
            for p in items
            if not p["used"]
            and p["person"] not in used_names
            and p.get("source") == "library"
            and p.get("doc_count", 0) > 0
        ),
        key=lambda x: (-x.get("doc_count", 0), x["person"]),
    )[:60]

    active_total = sum(p["active_task_count"] for p in items)
    return utils.get_response(
        200,
        {
            "persons": grid,
            "total": len(grid),
            "candidates": candidates,
            "active_task_total": active_total,
            "dynasties": person_store.dynasty_options(),
            "categories": person_store.category_options(),
            "sort_options": _DEFAULT_ORDER + sorted(_SORT_KEYS),
        },
    )


@router.get("/persons/suggest", response_model=BaseResponse, summary="Suggest persons when creating")
def persons_suggest(
    request: Request,
    name: str = Query("", description="输入的姓名/别名片段"),
):
    _ensure_projects_migrated()
    name = (name or "").strip()
    matches = []
    if name:
        for p in person_store.list_persons(keyword=name)[:30]:
            matches.append(
                {
                    "person": p["person"],
                    "dynasty": p.get("dynasty") or "",
                    "category": p.get("category") or "",
                    "in_library": p.get("source") == "library",
                    "doc_count": p.get("doc_count", 0),
                }
            )
    return utils.get_response(200, {"query": name, "candidates": matches})


@router.get("/tasks", response_model=BaseResponse, summary="List tasks")
def list_tasks(
    request: Request,
    status: str = Query("", description="状态筛选：processing|queued|awaiting_confirmation|completed|failed|cancelled|draft"),
    person: str = Query("", description="按人物筛选"),
    keyword: str = Query("", description="任务名/人物搜索"),
    limit: int = Query(200, ge=1, le=1000),
):
    _ensure_projects_migrated()
    rows = task_repo.list_tasks(status=status or "", person_name=person or "", keyword=keyword or "", limit=limit)
    return utils.get_response(200, {"tasks": rows, "total": len(rows)})


@router.get("/tasks/active", response_model=BaseResponse, summary="Active tasks drawer")
def active_tasks(request: Request):
    _ensure_projects_migrated()
    active = [
        t
        for t in task_repo.list_tasks(limit=100)
        if t.get("status") in task_repo.ACTIVE_STATUSES
    ]
    # 懒同步：用实时任务状态刷新进行中任务，回写任务表，使抽屉进度准确。
    merged = []
    for t in active:
        live = sm.state.get_task(t["task_id"]) or {}
        if live:
            try:
                task_repo.update_from_state(t["task_id"], live)
            except Exception:  # pragma: no cover - 回写失败不影响本次展示
                pass
            t = {
                **t,
                "progress": int(live.get("progress") or t.get("progress") or 0),
                "stage": live.get("failed_stage") or t.get("stage") or "",
                "state": live.get("state", t.get("status")),
            }
        merged.append(t)
    # 过滤掉已因实时状态转为终态、或不属于“进行中”集合（如历史草稿）的任务。
    merged = [t for t in merged if t.get("status") in task_repo.ACTIVE_STATUSES]
    return utils.get_response(200, {"tasks": merged, "count": len(merged)})
