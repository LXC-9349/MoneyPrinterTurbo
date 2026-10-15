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

import os
import threading
from datetime import datetime, timezone

from fastapi import Depends, Query, Request

from app.controllers import base
from app.controllers.v1.base import new_router
from app.models import const
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
        "task_count": s.get("task_count", 0),
        "video_count": s.get("video_count", 0),
        "article_count": s.get("article_count", 0),
        "article_enabled_count": s.get("article_enabled_count", 0),
        "active_task_count": s.get("active_task_count", 0),
        "awaiting_count": s.get("awaiting_count", 0),
        "last_updated": s.get("last_updated"),
        "adopted": bool(person.get("adopted")),
        # “已使用”：有任务/有文章/置顶/自建/采纳/有备注/有分组任一即进主网格；
        # 其余史料库人物只进入“候选”折叠区（归档人物在 archived 筛选下单独展示）。
        "used": (
            s.get("task_count", 0) > 0
            or s.get("video_count", 0) > 0
            or s.get("article_count", 0) > 0
            or s.get("active_task_count", 0) > 0
            or person.get("source") == "custom"
            or bool(person.get("pinned"))
            or bool(person.get("adopted"))
            or bool((person.get("note") or "").strip())
            or bool(person.get("groups"))
        ),
    }


def _is_candidate(p: dict) -> bool:
    """候选：史料库里有语料但从未使用的人物（无任务/文章、未采纳/置顶/备注/分组）。"""
    return (
        not p["used"]
        and p.get("source") == "library"
        and p.get("doc_count", 0) > 0
    )


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
    # keyword 不交给 list_persons（它只按主名子串匹配），而是下面在已使用网格内自己过滤；
    # 候选区另有自己的服务端搜索。这样 candidate_count 反映当前朝代/类别/分组下的全量候选。
    base = person_store.list_persons(
        dynasty=dynasty or "",
        category=category or "",
        group=group or "",
        include_hidden=include_hidden,
    )
    stats = task_repo.counts_by_person()
    decorated = [_decorate(p, stats) for p in base]

    # 主网格只展示“已使用”的人物；归档人物仅在 archived 筛选下出现。
    if status == "archived":
        items = [p for p in decorated if p.get("hidden")]
    elif status == "active":
        items = [p for p in decorated if p["used"] and p["active_task_count"] > 0]
    elif status == "videos":
        items = [p for p in decorated if p["used"] and p["video_count"] > 0]
    else:  # "" / 未知（包括旧 none）：主网格 = 已使用
        items = [p for p in decorated if p["used"]]

    if keyword:
        kw = keyword.strip().lower()
        items = [p for p in items if kw in p["person"].lower()]

    grid = sorted(items, key=lambda x: _sort_key(x, sort if sort in _SORT_KEYS else ""))
    candidate_count = sum(1 for p in decorated if _is_candidate(p))
    active_total = sum(p["active_task_count"] for p in decorated if p["used"])
    return utils.get_response(
        200,
        {
            "persons": grid,
            "total": len(grid),
            "candidate_count": candidate_count,
            "active_task_total": active_total,
            "dynasties": person_store.dynasty_options(),
            "categories": person_store.category_options(),
            "sort_options": _DEFAULT_ORDER + sorted(_SORT_KEYS),
        },
    )


@router.get("/persons/candidates", response_model=BaseResponse, summary="Paginated unused library candidates")
def persons_candidates(
    request: Request,
    keyword: str = Query("", description="姓名/别名关键词（服务端搜索）"),
    dynasty: str = Query("", description="朝代筛选"),
    category: str = Query("", description="类别筛选"),
    group: str = Query("", description="分组筛选"),
    offset: int = Query(0, ge=0, description="分页偏移"),
    limit: int = Query(50, ge=1, le=50, description="每页数量（≤５０）"),
):
    _ensure_projects_migrated()
    # 防御性钳制：无论 HTTP 校验还是直连调用，每页硬上限 50、偏移非负（§二 候选分页 ≤５０）。
    limit = max(1, min(int(limit), 50))
    offset = max(0, int(offset))
    base = person_store.list_persons(
        dynasty=dynasty or "",
        category=category or "",
        group=group or "",
        keyword=keyword or "",
        include_hidden=False,
    )
    stats = task_repo.counts_by_person()
    pool = [p for p in (_decorate(x, stats) for x in base) if _is_candidate(p)]
    pool.sort(key=lambda x: (-x.get("doc_count", 0), x["person"]))
    total = len(pool)
    page = pool[offset : offset + limit]
    return utils.get_response(
        200,
        {
            "candidates": page,
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": offset + limit < total,
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


@router.post("/tasks/{task_id}/cancel", response_model=BaseResponse, summary="Cancel an active task")
def cancel_task(request: Request, task_id: str):
    """顶栏抽屉“取消”：把活动任务固定为 cancelled 终态并从抽屉移除。

    里程碑 1 为协作式：写取消标记文件（供后续阶段边界/渲染层尊重）+ 标表 + best-effort
    刷新内存态；真正停下正在跑的阶段（硬中断）属里程碑 3。
    """
    _ensure_projects_migrated()
    marked = task_repo.mark_cancelled(task_id)
    # 协作式取消标记：与 video_project 渲染层同一约定，阶段边界可据此提前退出。
    try:
        marker_dir = utils.task_dir(task_id)
        os.makedirs(marker_dir, exist_ok=True)
        with open(os.path.join(marker_dir, "cancel.json"), "w", encoding="utf-8") as fh:
            fh.write('{"cancelled": true}')
    except OSError:  # pragma: no cover - 标记写入失败不阻断“从抽屉移除”
        pass
    # best-effort 刷新内存态：仅对已存在记录补写，不重建已删除任务。
    try:
        sm.state.patch_task(task_id, state=const.TASK_STATE_CANCELLED)
    except Exception:  # pragma: no cover - 内存态不可用不影响任务表已标取消
        pass
    return utils.get_response(200, {"task_id": task_id, "cancelled": True, "marked": marked})
