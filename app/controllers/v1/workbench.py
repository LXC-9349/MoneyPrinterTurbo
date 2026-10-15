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

import json
import os
import threading
from datetime import datetime, timezone
from urllib.parse import unquote

from fastapi import Depends, File, Query, Request, UploadFile

from app.controllers import base
from app.controllers.v1.base import new_router
from app.models import const
from app.models.exception import HttpException
from app.models.schema import (
    ArticleCreateRequest,
    ArticlePatchRequest,
    BaseResponse,
    DraftSaveRequest,
    FactCreateRequest,
    FactPatchRequest,
    LibraryLinkPatchRequest,
    PersonAliasRequest,
    PersonAssetPatchRequest,
)
from app.services import (
    article_repo,
    asset_registry,
    person_repo,
    person_store,
    state as sm,
    task,
    task_repo,
)
from app.utils import utils

router = new_router(dependencies=[Depends(base.verify_token)])
# 独立子前缀：遗留 video.py 已占用 /api/v1/tasks 与 /api/v1/tasks/{task_id}（实时
# 内存态查询），工作台的任务观测接口若同名会被先注册者遮蔽 / 误匹配为 404。
router.prefix = "/api/v1/workbench"

# projects.json → tasks 的一次性迁移：首次访问工作台时惰性执行，幂等，仅一次。
# 同时补建历史孤儿任务的人物档案（里程碑 2 收尾 #7），让海明威类任务能在卡片上看到。
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
        try:
            task_repo.backfill_orphan_persons()
        except Exception:  # pragma: no cover - 补档失败不阻断页面，下次再试
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


# ── 史料库软引用文章（里程碑 2 收尾 #6）───────────────────────────
def _soft_library_articles(person: str, hard_articles: list[dict]) -> list[dict]:
    """人物详情用：史料库关联文章的“软引用行”（不拷贝正文）。

    已硬添加为正式文章（content_ref=library:<doc_id>）的文档从软引用中排除，
    避免双计；开关/权重取 library_links 已设值，未设置时低可信来源默认不启用。
    文库不可用时降级为空列表，不影响详情页其余数据。
    """
    try:
        from app.services import library

        docs = library.person_soft_articles(person)
    except Exception:  # pragma: no cover - 文库异常不阻断详情
        return []
    if not docs:
        return []
    hard_ids = {
        (a.get("content_ref") or "")[len("library:") :]
        for a in hard_articles
        if (a.get("content_ref") or "").startswith("library:")
    }
    states = person_repo.get_link_states(person)
    rows = []
    for d in docs:
        did = d["doc_id"]
        if did in hard_ids:
            continue
        st = states.get(did) or {}
        enabled = st.get("enabled")
        if enabled is None:
            enabled = d["reliability"] != "low"
        weight = st.get("weight")
        rows.append(
            {
                **d,
                "enabled": bool(enabled),
                "weight": float(weight) if weight is not None else 1.0,
            }
        )
    return rows


def _merge_soft_counts(items: list[dict]) -> None:
    """把软引用文章数合入卡片 article_count/article_enabled_count（原地改）。

    只处理已展示集合（几人到几百人），零 N+1：批量取软引文档/硬引集合/开关状态
    各一次查询。used 判定不受影响（已在 _decorate 里基于硬文章算定）。
    """
    names = [p.get("person", "") for p in items if p.get("person")]
    if not names:
        return
    try:
        from app.services import library

        soft = library.soft_docs_for_names(names)
    except Exception:  # pragma: no cover - 文库异常时卡片保持硬文章计数
        return
    if not soft:
        return
    overrides = person_repo.bulk_link_states(names)
    hard_refs = article_repo.library_doc_ids_by_person()
    for p in items:
        docs = soft.get(p.get("person") or "")
        if not docs:
            continue
        excl = hard_refs.get(p.get("person") or "") or set()
        total = 0
        enabled_n = 0
        for d in docs:
            if d["doc_id"] in excl:
                continue
            total += 1
            ov = overrides.get((p["person"], d["doc_id"])) or {}
            e = ov.get("enabled")
            enabled_n += int(e) if e is not None else (1 if d["reliability"] != "low" else 0)
        if total:
            p["article_count"] = p.get("article_count", 0) + total
            p["article_enabled_count"] = p.get("article_enabled_count", 0) + enabled_n
            p["library_article_count"] = total
            p["library_article_enabled_count"] = enabled_n


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

    # 史料库软引用文章计入卡片文章数（在排序前合并，按文章数排序用得到含软引的值；
    # used 判定已在 _decorate 里基于硬文章算好，不受影响）。
    _merge_soft_counts(items)
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
    _merge_soft_counts(page)
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
                # 运行中阶段由进度边界推导（先修3）；失败时回退 failed_stage / 表列。
                "stage": (
                    live.get("failed_stage")
                    or task._stage_from_progress(live.get("progress"))
                    or t.get("stage")
                    or ""
                ),
                "state": live.get("state", t.get("status")),
            }
        merged.append(t)
    # 过滤掉已因实时状态转为终态、或不属于“进行中”集合（如历史草稿）的任务。
    merged = [t for t in merged if t.get("status") in task_repo.ACTIVE_STATUSES]
    return utils.get_response(200, {"tasks": merged, "count": len(merged)})


@router.post("/tasks/{task_id}/cancel", response_model=BaseResponse, summary="Cancel an active task")
def cancel_task(request: Request, task_id: str):
    """顶栏抽屉“取消”：把活动任务固定为 cancelled 终态并从抽屉移除。

    写取消标记文件（cancel.json）+ 标表 + best-effort 刷新内存态。流水线在
    每个阶段边界调用 task._raise_if_cancelled 读取该标记，命中即提前停线、清理
    临时产物并固定 cancelled（不再进入下一阶段的 LLM/TTS/素材/合成），取消真正
    生效；task_repo.update_from_state 另有 status != 'cancelled' 守卫，终态不被覆盖。
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


def _http_error(request: Request, status_code: int, message: str) -> HttpException:
    return HttpException(task_id=base.get_task_id(request), status_code=status_code, message=message)


def _profile_or_404(request: Request, name: str) -> dict:
    profile = person_store.get_person(name)
    if profile is None:
        raise _http_error(request, 404, f"person not found: {name}")
    return profile


# 摄影术发明（1839）之前终结的中国朝代：古代人物详情页据此提示
# “此人无真实照片”，图片大概率系后人绘像/影视取材，引导上传或检索画像。
# 民国及以后、外国人物不提示（同时涵盖用户上传自有图片的判定依据）。
_PRE_PHOTO_DYNASTIES = {
    "先秦",
    "秦汉",
    "魏晋南北朝",
    "隋唐",
    "五代十国",
    "唐",
    "宋",
    "元",
    "明",
    "清",
}


def _no_authentic_photo(profile: dict) -> bool:
    return (profile.get("dynasty") or "").strip() in _PRE_PHOTO_DYNASTIES


def _videos_from_tasks(rows: list[dict]) -> list[dict]:
    """从已完成任务的 artifacts_json 提取**最终成片**（final-*.mp4）路径。

    只登记 `videos`（流水线写入的最终成片）；`combined_videos` 是加字幕前的
    中间合成件，属于过程产物，不得登记为人物的成片、也不得计入视频数。仅改
    视图读取，不触碰磁盘文件与 artifacts_json 原始记录（保留中间件供排查）。
    """
    videos: list[dict] = []
    for t in rows:
        if t.get("status") != "completed":
            continue
        try:
            art = json.loads(t.get("artifacts_json") or "{}")
        except (ValueError, TypeError):
            art = {}
        for path in (art.get("videos") or []):
            if path:
                videos.append(
                    {
                        "task_id": t.get("task_id"),
                        "path": path,
                        "created_at": t.get("completed_at") or t.get("updated_at"),
                    }
                )
    return videos


@router.get(
    "/persons/{name}/detail", response_model=BaseResponse, summary="Person detail aggregation"
)
def person_detail(request: Request, name: str):
    """人物详情页一次返回：头部档案 + 文章/任务/成片/事实/图片素材 + 进行中任务。

    各自来自已有单一事实源（task_repo/article_repo/person_repo），一次聚合、
    不做逐人 N+1。归档/未知人物 404。
    """
    _ensure_projects_migrated()
    person = unquote(name).strip()
    profile = _profile_or_404(request, person)
    tasks = task_repo.list_tasks(person_name=person, limit=500)
    active = [t for t in tasks if t.get("status") in task_repo.ACTIVE_STATUSES]
    articles = article_repo.list_articles(person)
    library_articles = _soft_library_articles(person, articles)
    assets = person_repo.list_person_assets(person)
    # 旧覆盖层 key_facts 字符串数组首次访问时一次性迁入 facts 表（仅当无行）；
    # 之后事实标签的唯一真源是 facts 表，支持增删改与核验/确认态。
    person_repo.import_overlay_facts(person, profile.get("key_facts") or [])
    facts = person_repo.list_facts(person)
    videos = _videos_from_tasks(tasks)
    return utils.get_response(
        200,
        {
            "profile": profile,
            "articles": articles,
            "library_articles": library_articles,
            "tasks": tasks,
            "videos": videos,
            "facts": facts,
            "assets": assets,
            "aliases": person_repo.list_aliases(person),
            "no_authentic_photo": _no_authentic_photo(profile),
            "active_tasks": active,
            "summary": {
                "article_count": len(articles) + len(library_articles),
                "article_enabled_count": sum(1 for a in articles if a.get("enabled"))
                + sum(1 for r in library_articles if r["enabled"]),
                "library_article_count": len(library_articles),
                "library_article_enabled_count": sum(
                    1 for r in library_articles if r["enabled"]
                ),
                "task_count": len(tasks),
                "video_count": len({v["path"] for v in videos}),
                "fact_count": len(facts),
                "asset_count": len(assets),
                "active_count": len(active),
            },
        },
    )


@router.get(
    "/persons/{name}/library-articles",
    response_model=BaseResponse,
    summary="Searchable library docs for a person (to add as articles)",
)
def person_library_articles(request: Request, name: str):
    """史料库可检索添加的文章（返回 doc_id/标题/类别/字数/可信度 + 摘要窗口）。"""
    _ensure_projects_migrated()
    person = unquote(name).strip()
    profile = _profile_or_404(request, person)
    docs = profile.get("docs") or []
    doc_ids = [d.get("doc_id") for d in docs if d.get("doc_id")]
    meta = {d.get("doc_id"): d for d in docs}
    bodies: dict[str, str] = {}
    try:
        from app.services import library

        for d in library.get_docs(doc_ids):
            bodies[d["doc_id"]] = d.get("body") or ""
    except Exception:  # pragma: no cover - 文库不可用时仅返回元数据
        pass
    items = []
    for did in doc_ids:
        body = bodies.get(did, "")
        m = meta.get(did, {})
        items.append(
            {
                "doc_id": did,
                "title": m.get("title") or "",
                "category": m.get("category") or "",
                "n_chars": m.get("n_chars") or len(body),
                "excerpt": (body or "").strip().replace("\n", " ")[:160],
                "reliability": "high" if profile.get("reliable_high") else "medium",
            }
        )
    return utils.get_response(200, {"person": person, "documents": items})


@router.get(
    "/persons/{name}/articles", response_model=BaseResponse, summary="List a person's articles"
)
def list_person_articles(request: Request, name: str):
    _ensure_projects_migrated()
    person = unquote(name).strip()
    return utils.get_response(200, {"articles": article_repo.list_articles(person)})


@router.post(
    "/persons/{name}/articles", response_model=BaseResponse, summary="Add an article"
)
def add_person_article(request: Request, name: str, body: ArticleCreateRequest):
    _ensure_projects_migrated()
    person = unquote(name).strip()
    source = (body.source or "paste").strip()
    if source == "library":
        if not body.doc_id:
            raise _http_error(request, 400, "library article requires doc_id")
        article = article_repo.add_article(
            person,
            body.title,
            source="library",
            content_ref=f"library:{body.doc_id}",
            excerpt=body.excerpt,
            reliability=body.reliability,
            weight=body.weight,
            enabled=body.enabled,
        )
    else:
        content = body.content or ""
        if not content.strip():
            raise _http_error(request, 400, "article content is required")
        first_line = content.strip().splitlines()[0][:40]
        article = article_repo.add_article(
            person,
            body.title or first_line or "未命名文章",
            source=source if source in ("paste", "upload") else "paste",
            content=content,
            reliability=body.reliability,
            weight=body.weight,
            enabled=body.enabled,
        )
    return utils.get_response(200, article)


@router.patch(
    "/articles/{article_id}", response_model=BaseResponse, summary="Update article (enabled/weight)"
)
def patch_article(request: Request, article_id: int, body: ArticlePatchRequest):
    _ensure_projects_migrated()
    if body.enabled is not None:
        article_repo.set_enabled(article_id, body.enabled)
    if body.weight is not None:
        article_repo.set_weight(article_id, body.weight)
    article = article_repo.get_article(article_id)
    if article is None:
        raise _http_error(request, 404, f"article not found: {article_id}")
    return utils.get_response(200, article)


@router.delete(
    "/articles/{article_id}", response_model=BaseResponse, summary="Delete an article"
)
def delete_article(request: Request, article_id: int):
    _ensure_projects_migrated()
    deleted = article_repo.delete_article(article_id)
    return utils.get_response(200, {"article_id": article_id, "deleted": bool(deleted)})


@router.patch(
    "/persons/{name}/library-links/{doc_id}",
    response_model=BaseResponse,
    summary="Toggle/weight a library soft-reference article",
)
def patch_library_link(request: Request, name: str, doc_id: str, body: LibraryLinkPatchRequest):
    """软引用行的“用于生成”开关/权重：状态存 library_links（人物维度）。

    仅接受该人物当前软引用列表里的 doc_id（已硬添加的会被排除→ 404）；
    人物未物化时 upsert 自动建档（与事实/别名端点一致）。
    """
    _ensure_projects_migrated()
    person = unquote(name).strip()
    did = unquote(doc_id).strip()
    if body.enabled is None and body.weight is None:
        raise _http_error(request, 400, "enabled or weight is required")
    _profile_or_404(request, person)
    try:
        from app.services import library

        soft_ids = {d["doc_id"]: d for d in library.person_soft_articles(person)}
    except Exception:  # pragma: no cover - 文库不可用时无任何软引用可操作
        soft_ids = {}
    if did not in soft_ids:
        raise _http_error(request, 404, f"library article not linked to {person}: {did}")
    state = person_repo.upsert_link_state(
        person,
        did,
        enabled=body.enabled,
        weight=body.weight,
        source_title=soft_ids[did].get("title") or "",
    )
    return utils.get_response(
        200,
        {
            "person": person,
            "doc_id": did,
            "enabled": bool(state["enabled"]) if state["enabled"] is not None else None,
            "weight": state["weight"],
        },
    )


@router.post("/drafts", response_model=BaseResponse, summary="Save a task draft")
def save_draft_endpoint(request: Request, body: DraftSaveRequest):
    _ensure_projects_migrated()
    draft_id = (body.draft_id or "").strip() or f"draft_{utils.get_uuid()}"
    # 与提交生成同一建档路径：草稿人物不存在时自动建档（自建），不阻断保存。
    person = (body.person or "").strip()
    if person:
        try:
            person_store.ensure_person_for_task(person)
        except Exception:  # pragma: no cover - 旁路建档
            pass
    task_repo.save_draft(
        draft_id,
        person_name=body.person,
        title=body.title or body.person,
        params=body.params,
    )
    row = task_repo.get_task(draft_id)
    return utils.get_response(200, row)


@router.delete("/drafts/{draft_id}", response_model=BaseResponse, summary="Delete a draft")
def delete_draft_endpoint(request: Request, draft_id: str):
    _ensure_projects_migrated()
    deleted = task_repo.delete_draft(draft_id)
    return utils.get_response(200, {"draft_id": draft_id, "deleted": bool(deleted)})


# ── 别名（里程碑 2 收尾 #5 编辑资料弹窗；存 mpt.sqlite person_aliases）───
@router.post(
    "/persons/{name}/aliases", response_model=BaseResponse, summary="Add a person alias"
)
def add_person_alias(request: Request, name: str, body: PersonAliasRequest):
    """新增别名：重复添加幂等；人物未物化先建档。"""
    _ensure_projects_migrated()
    person = unquote(name).strip()
    alias = (body.alias or "").strip()
    if not alias or alias == person:
        raise _http_error(request, 400, "alias is required")
    created = person_repo.create_or_get_person(person)
    person_repo.add_alias(created["public_id"], alias, source="manual")
    return utils.get_response(
        200, {"person": person, "aliases": person_repo.list_aliases(person)}
    )


@router.delete(
    "/persons/{name}/aliases/{alias}", response_model=BaseResponse, summary="Remove a person alias"
)
def delete_person_alias(request: Request, name: str, alias: str):
    """删除别名；主名不可删。"""
    _ensure_projects_migrated()
    person = unquote(name).strip()
    removed = person_repo.delete_alias(person, unquote(alias).strip())
    if not removed:
        raise _http_error(request, 404, f"alias not found: {alias}")
    return utils.get_response(
        200, {"person": person, "aliases": person_repo.list_aliases(person)}
    )


# ── 事实 CRUD（里程碑 2 收尾 #4：增删改 + 核验/人工确认）─────────────────
@router.post(
    "/persons/{name}/facts", response_model=BaseResponse, summary="Add a fact"
)
def add_person_fact(request: Request, name: str, body: FactCreateRequest):
    """新增事实：人物未物化先建档；人工录入视为已核验且已确认。"""
    _ensure_projects_migrated()
    person = unquote(name).strip()
    statement = (body.statement or "").strip()
    if not statement:
        raise _http_error(request, 400, "fact statement is required")
    created = person_repo.create_or_get_person(person)
    ordinal = len(person_repo.list_facts(person))
    fact_id = person_repo.add_fact(
        created["public_id"],
        statement,
        ordinal=ordinal,
        source="manual",
        verified=1,
        human_confirmed=1,
        source_ref=(body.source_ref or "").strip(),
    )
    return utils.get_response(
        200,
        {
            "fact_id": fact_id,
            "ordinal": ordinal,
            "statement": statement,
            "source": "manual",
            "source_ref": (body.source_ref or "").strip(),
            "verified": 1,
            "human_confirmed": 1,
            "edited_by_user": 0,
        },
    )


@router.patch(
    "/facts/{fact_id}", response_model=BaseResponse, summary="Update a fact"
)
def patch_person_fact(request: Request, fact_id: int, body: FactPatchRequest):
    """编辑/采纳事实：改正文自动标 edited_by_user；采纳未核验项置
    human_confirmed=1。"""
    _ensure_projects_migrated()
    row = person_repo.update_fact(
        fact_id,
        statement=(body.statement.strip() if body.statement else None),
        verified=None if body.verified is None else int(body.verified),
        human_confirmed=None if body.human_confirmed is None else int(body.human_confirmed),
    )
    if row is None:
        raise _http_error(request, 404, f"fact not found: {fact_id}")
    return utils.get_response(200, row)


@router.delete(
    "/facts/{fact_id}", response_model=BaseResponse, summary="Delete (ignore) a fact"
)
def delete_person_fact(request: Request, fact_id: int):
    """删除/忽略单条事实（未核验项的“忽略”即移除）。"""
    _ensure_projects_migrated()
    deleted = person_repo.delete_fact(fact_id)
    if not deleted:
        raise _http_error(request, 404, f"fact not found: {fact_id}")
    return utils.get_response(200, {"fact_id": fact_id, "deleted": True})


# ── 人物图片素材操作（里程碑 2 收尾 #3：封面/排除/上传）────────────────
_IMAGE_UPLOAD_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
_MAX_IMAGE_UPLOAD_BYTES = 20 * 1024 * 1024


@router.post(
    "/persons/{name}/assets/{asset_id}/cover",
    response_model=BaseResponse,
    summary="Set a person image asset as the cover",
)
def set_person_asset_cover(request: Request, name: str, asset_id: int):
    """设为封面：每人唯一封面，其余清零（person_repo 事务内完成）。"""
    _ensure_projects_migrated()
    person = unquote(name).strip()
    ok = person_repo.set_person_asset_cover(person, asset_id)
    if not ok:
        raise _http_error(request, 404, f"asset not linked to person: {person}/{asset_id}")
    return utils.get_response(200, {"person": person, "asset_id": asset_id, "is_cover": True})


@router.patch(
    "/persons/{name}/assets/{asset_id}",
    response_model=BaseResponse,
    summary="Update person asset state (exclude/restore)",
)
def patch_person_asset(request: Request, name: str, asset_id: int, body: PersonAssetPatchRequest):
    """排除/恢复素材：写人物级 state（excluded 即人物级黑名单，检索侧强制
    跳过在里程碑 4）；排除时自动取消封面，避免“被排除的图仍是封面”。"""
    _ensure_projects_migrated()
    person = unquote(name).strip()
    state = (body.state or "").strip()
    try:
        ok = person_repo.set_person_asset_state(person, asset_id, state)
    except ValueError as exc:
        raise _http_error(request, 400, str(exc))
    if not ok:
        raise _http_error(request, 404, f"asset not linked to person: {person}/{asset_id}")
    return utils.get_response(200, {"person": person, "asset_id": asset_id, "state": state})


@router.post(
    "/persons/{name}/assets/upload",
    response_model=BaseResponse,
    summary="Upload a custom image asset for the person",
)
async def upload_person_image(request: Request, name: str, file: UploadFile = File(...)):
    """上传自有图片：落盘 storage/tasks/person_images/（经 /tasks 静态挂载可访问），
    按内容哈希去重登记 assets 并关联人物；人物不存在时自动建档。"""
    _ensure_projects_migrated()
    person = unquote(name).strip()
    if not person:
        raise _http_error(request, 400, "person name is required")
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in _IMAGE_UPLOAD_EXTS:
        raise _http_error(request, 400, "仅支持图片文件（jpg/jpeg/png/webp/gif/bmp）")
    payload = await file.read()
    if not payload:
        raise _http_error(request, 400, "上传文件为空")
    if len(payload) > _MAX_IMAGE_UPLOAD_BYTES:
        raise _http_error(request, 400, "图片过大（限制 20MB）")
    target_dir = os.path.join(utils.task_dir(), "person_images")
    os.makedirs(target_dir, exist_ok=True)
    # 不可变 UUID 存储键：同名重复上传不互相覆盖，磁盘去重交给内容哈希。
    target = os.path.join(target_dir, f"{utils.get_uuid()}{ext}")
    try:
        with open(target, "wb") as fh:
            fh.write(payload)
        asset = asset_registry.register_uploaded_image(
            person, target, source_title=(file.filename or "").strip()[:200]
        )
    except (ValueError, FileNotFoundError, OSError) as exc:
        raise _http_error(request, 500, f"image upload failed: {exc}")
    return utils.get_response(200, asset)
