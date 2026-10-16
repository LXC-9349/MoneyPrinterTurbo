"""historical_archive material provider for biography videos.

Flow per script shot (mirrors the existing on-demand providers in
app/services/material.py so paid/generated sources and this one share one
contract):

  1. Build the query: person profile era keywords (local library) + the shot's
     visual_query, falling back to the person's English Wikipedia title.
  2. Search Commons (priority a) -> LOC (c). Both return licensed images only.
  3. Download, render each still into a clip (``render_image_zoom_video``) so
     the concat engine only ever sees mp4s.
  4. Record license/provenance per file for the compliance sidecar.

Any step failing degrades: a shot with no archive image returns nothing and
the caller falls back to stock b-roll for that shot. A completely dead
archive API never raises out of this module.
"""
from __future__ import annotations

import os
from typing import Any

from loguru import logger

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect
from app.services import video as video_service
from app.services.archives import (
    fetch_person_summary,
    resolve_english_title,
    search_loc_images,
    search_wikimedia_images,
)
from app.utils import utils


def enabled() -> bool:
    return bool(config.app.get("historical_archive_enabled", True))


# 复用时循环的 Ken Burns 方向（首次使用为默认居中放大，不入此列）。
_REUSE_DIRECTIONS = ("pan_right", "pan_left", "zoom_out", "pan_down", "pan_up")

# 古代人物素材分类关键词（中/英）→ visual 子类，用于界面标注与 visual_type。
_SUBTYPE_KEYWORDS = (
    ("portrait", (
        "portrait", "headshot", "bust", "likeness", "画像", "头像", "半身像", "写真",
    )),
    ("sculpture", (
        "statue", "sculpture", "monument", "bust", "雕塑", "雕像", "塑像", "纪念碑",
    )),
    ("painting", (
        "painting", "artwork", "illustration", "woodcut", "engraving", "scroll",
        "绘画", "画作", "壁画", "插图", "版画", "画卷",
    )),
    ("map", (
        "map", "cartography", "atlas", "地图", "舆图", "疆域图",
    )),
    ("artifact", (
        "artifact", "artefact", "relic", "pottery", "coin", "bronze", "manuscript",
        "文物", "器物", "青铜", "钱币", "陶器", "手稿", "碑刻",
    )),
    ("site", (
        "ruin", "ruins", "archaeological site", "excavation", "temple", "palace",
        "tomb", "遗址", "遗迹", "考古", "宫殿", "陵墓", "寺庙",
    )),
)


# 主题性 b-roll 场景词（批次 B 兜底）：绝不用人名搜库存网站，只用
# 中性场景词。按人物时代/领域选一档，避免库存站拿“曹操”去匹配到无关素材。
_THEMATIC_BROLL = {
    "chinese": (
        "ancient chinese battlefield", "chinese ink painting", "bamboo scrolls",
        "chinese calligraphy brush", "ancient chinese palace", "great wall of china",
        "silk road caravan", "ancient chinese bronze",
    ),
    "science": (
        "physics laboratory", "chalkboard equations", "antique telescope",
        "old scientific instruments", "stack of old books", "chemistry glassware",
    ),
    "european": (
        "medieval castle", "old library manuscripts", "european oil painting",
        "candlelit study room", "ancient roman ruins",
    ),
    "default": (
        "old paper texture", "candlelight darkness", "ancient manuscript",
        "ink and brush", "dusty library shelf", "historical map parchment",
    ),
}


def _broll_theme(person: str, era_keywords: list[str]) -> str:
    text = f"{person} {' '.join(era_keywords or [])}".lower()
    if any(c >= "\u4e00" and c <= "\u9fff" for c in person) or any(
        k in text for k in ("china", "chinese", "han ", "tang ", "three kingdoms", "dynasty", "emperor")
    ):
        return "chinese"
    if any(k in text for k in ("physic", "scient", "math", "chemistry", "biolog", "nobel", "relativity", "astronom")):
        return "science"
    if any(k in text for k in ("europe", "medieval", "roman", "british", "french", "german", "italian")):
        return "european"
    return "default"


def scene_fallback_terms(
    person: str,
    video_terms: list[str] | None = None,
    *,
    limit: int = 8,
) -> list[str]:
    """为主题性 b-roll 兜底构造场景词检索词（批次 B）。

    硬规则：绝不用人名搜库存网站。先把 video_terms 里的人名/别名剔除，
    只保留通用场景词；再并入按时代/领域选定的中性主题词。任何失败都
    退化到 default 主题词，不抛出。
    """
    person = (person or "").strip()
    aliases: list[str] = []
    era_keywords: list[str] = []
    try:
        ctx = person_gate_context(person)
        aliases = ctx.get("aliases", []) or []
        era_keywords = ctx.get("era_keywords", []) or []
    except Exception:  # noqa: BLE001
        aliases, era_keywords = [], []

    strip_names = {person.lower()} | {a.lower() for a in aliases if a}
    scene_terms: list[str] = []
    for term in video_terms or []:
        cleaned = str(term or "").strip()
        # 大小写不敏感地剔除人名/别名（库存站检索对大小写不敏感，
        # "Cao Cao portrait" 里的 "cao cao" 同样必须剔除）。
        low = cleaned.lower()
        for nm in strip_names:
            if nm:
                low = low.replace(nm, " ")
        cleaned = " ".join(low.split())
        # 剔除后剩下的才是真正的场景词（至少一个有意义的英文词）。
        if cleaned and cleaned not in strip_names and len(cleaned) >= 3:
            scene_terms.append(cleaned)

    theme = _THEMATIC_BROLL[_broll_theme(person, era_keywords)]
    out: list[str] = []
    for t in scene_terms + list(theme):
        if t not in out:
            out.append(t)
        if len(out) >= limit:
            break
    return out or list(theme[:limit])


def classify_archive_subtype(item: MaterialInfo) -> str:
    """将档案素材分为画像/雕塑/绘画/地图/文物/遗址（批次 B，古代人物标注）。

    依据 category/description/asset_id 关键词判定；无命中返回 ""（普通历史图）。
    """
    si = item.source_info or {}
    text = " ".join(
        str(si.get(k) or "") for k in ("category", "description", "asset_id", "credit")
    ).lower()
    if not text.strip():
        return ""
    for subtype, keywords in _SUBTYPE_KEYWORDS:
        if any(kw in text for kw in keywords):
            return subtype
    return ""


def _relevance_threshold() -> str:
    """三档相关性门槛（strict/standard/loose），"off"/"" 关闭过滤。

    默认标准档；由 config.app.material_relevance_threshold 覆盖，便于按任务
    参数在确认屏切换宽严。
    """
    th = str(config.app.get("material_relevance_threshold", "standard") or "standard")
    return th.strip().lower()


def person_gate_context(person: str) -> dict:
    """为相关性门槛收集上下文：主名 + 别名 + 英文名 + 时代关键词 + QID。

    别名先取本地人物库（person_repo.list_aliases），再并入 Wikidata 的
    中英文标签与别名（批次 B）；英文名走 Wikipedia 解析；时代关键词取
    史料库档案的 era_en。QID 用于结构化取材（分类树/depicts）。任一环节
    失败都不抛出，只退化可用字段——门槛本身对缺失字段宽容。
    """
    person = (person or "").strip()
    aliases: list[str] = []
    era_keywords: list[str] = []
    qid = ""
    if not person:
        return {"name": person, "aliases": aliases, "era_keywords": era_keywords, "qid": qid}

    try:
        from app.services import person_repo

        aliases = [a["alias"] for a in person_repo.list_aliases(person) if a.get("alias")]
    except Exception:  # noqa: BLE001 — 别名库不可用不应阻断检索
        aliases = []

    # Wikidata 解析：QID + 中英文标签/别名并入别名集合（写回 persons.qid 缓存）。
    try:
        from app.services.archives import wikidata

        resolved = wikidata.resolve_and_cache_person(person)
        qid = resolved.get("qid", "") or ""
        for a in resolved.get("aliases", []) or []:
            if a and a not in aliases:
                aliases.append(a)
    except Exception:  # noqa: BLE001
        qid = ""

    en = resolve_english_title(person) or ""
    if en and en not in aliases and en != person:
        aliases.append(en)

    try:
        from app.services import library

        era = library.era_keywords(person, "") or ""
        era_keywords = [p.strip() for p in era.split(",") if p.strip()]
    except Exception:  # noqa: BLE001
        era_keywords = []

    return {"name": person, "aliases": aliases, "era_keywords": era_keywords, "qid": qid}


def apply_relevance_gate(
    person: str,
    items: list[MaterialInfo],
    *,
    threshold: str | None = None,
) -> tuple[list[MaterialInfo], list[dict]]:
    """把候选过一遍相关性门槛，返回 (通过, 被丢弃记录)。

    threshold="off"/"" 时全部通过（不改动既有行为）。被丢弃记录含
    provider/title/source_page/score/reason/url，供任务提醒逐条列出。
    """
    th = (threshold if threshold is not None else _relevance_threshold()) or "standard"
    if th in ("off", "none", "disabled"):
        return list(items), []
    ctx = person_gate_context(person)
    from app.services.relevance_gate import filter_candidates

    return filter_candidates(
        items,
        person_name=ctx["name"],
        aliases=ctx["aliases"],
        era_keywords=ctx["era_keywords"],
        threshold=th,
    )


def _download_image(url: str, save_dir: str) -> str:
    """Download one archive still to disk (material.save_image)."""
    from app.services import material

    return material.save_image(url, save_dir=save_dir)


def _asset_identity(item: MaterialInfo) -> str:
    """素材身份键：优先用 asset_id 去扩展名/大小写，回退到去 query 的 URL。

    近重复去重据此判定“同一张照片”，而非依赖每次搜索都不同的缩略图 URL。
    """
    si = item.source_info or {}
    asset = str(si.get("asset_id") or "").strip()
    if asset:
        base = os.path.splitext(asset)[0].lower().replace("file:", "").strip()
        if base:
            return base
    return (item.url or "").split("?")[0].rstrip("/").lower()


def _is_excluded(item: MaterialInfo, excluded_keys: set[str] | None) -> bool:
    """人物级黑名单命中判定（M4#3）：source_page/asset_id/url 任一命中即跳过。"""
    if not excluded_keys:
        return False
    si = item.source_info or {}
    for v in (si.get("source_page"), si.get("asset_id"), item.url):
        s = str(v or "").strip().lower()
        if s and s in excluded_keys:
            return True
    return False


# 结构化素材（分类树/depicts/P18）不依赖逐镜 extra_query，按人物进程级记忆，
# 避免逐镜重复拉 Wikidata/Commons。值为已解析的 MaterialInfo 列表。
_STRUCTURAL_CACHE: dict[str, list[MaterialInfo]] = {}


def structural_person_materials(person: str, *, qid: str = "") -> list[MaterialInfo]:
    """人物的结构化相关素材池（批次 B）：Wikidata QID → P18/depicts/分类树。

    按人物记忆（不依赖逐镜查询词）。QID 未传时自行解析。任何失败降级为空。
    """
    person = (person or "").strip()
    if not person:
        return []
    if person in _STRUCTURAL_CACHE:
        return _STRUCTURAL_CACHE[person]
    items: list[MaterialInfo] = []
    try:
        from app.services.archives import commons, wikidata

        resolved_qid = qid or wikidata.resolve_qid(person)
        if not resolved_qid and not person.isascii():
            en = resolve_english_title(person)
            if en:
                resolved_qid = wikidata.resolve_qid(en, lang="en")
        if resolved_qid:
            items = commons.collect_qid_materials(resolved_qid, limit=40)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"historical_archive: structural sourcing failed for {person!r}: "
            f"{type(exc).__name__}"
        )
        items = []
    _STRUCTURAL_CACHE[person] = items
    return items


def find_person_images(
    person: str,
    *,
    extra_query: str = "",
    limit: int = 6,
    discarded_out: list | None = None,
    threshold: str | None = None,
    excluded_keys: set[str] | None = None,
) -> list[MaterialInfo]:
    """Candidate archive images for a person (no downloads).

    ``extra_query`` narrows by scene (e.g. "portrait", "laboratory"), which
    keeps shots visually distinct instead of repeating one headshot.

    检索结果先过相关性门槛（M4#2），再按人物级黑名单（M4#3，
    ``excluded_keys``）与素材身份近重复去重（同一照片不同尺寸/文件名只留一）。
    被丢弃项追写到 ``discarded_out``（供任务提醒）。搜索时适当超采（limit*2）
    再过滤，避免门槛/黑名单把池子抽干。
    """
    if not enabled():
        return []
    person = (person or "").strip()
    if not person:
        return []

    # English title first: Commons coverage is far better in English.
    en_title = resolve_english_title(person) or person
    era = ""
    try:
        from app.services import library

        era = library.era_keywords(person, "")
    except Exception:
        era = ""

    queries: list[str] = []
    for base in (en_title, person):
        if not base:
            continue
        if extra_query:
            queries.append(f"{base} {extra_query}".strip())
        if era:
            queries.append(f"{base} {era}".strip())
        queries.append(base)

    fetch_limit = max(limit * 2, limit)
    seen_urls: set[str] = set()
    seen_ids: set[str] = set()
    raw: list[MaterialInfo] = []

    def _collect(items):
        for item in items:
            if item.url in seen_urls:
                continue
            ident = _asset_identity(item)
            if ident and ident in seen_ids:
                # 近重复：同一照片的不同尺寸/镜像命中只保留首个。
                continue
            if _is_excluded(item, excluded_keys):
                continue
            seen_urls.add(item.url)
            if ident:
                seen_ids.add(ident)
            raw.append(item)

    # 结构化素材优先（批次 B）：按分类树/depicts/P18 取材，不依赖文件名
    # 子串匹配，即使中文/不透明标题也能拿到相关图。放在最前，让池子先
    # 被高置信度的相关素材填满。
    _collect(structural_person_materials(person))

    for query in queries:
        _collect(search_wikimedia_images(query, limit=fetch_limit))
    # Supplementary source: LOC has strong US/European photographic archives.
    for base_q in (en_title, person):
        if not base_q or len(raw) >= fetch_limit:
            continue
        _collect(search_loc_images(base_q, limit=limit))

    # 相关性门槛：低于阈值的候选丢弃并记录，不静默当成可用素材循环使用。
    passed, discarded = apply_relevance_gate(person, raw, threshold=threshold)
    if discarded_out is not None:
        for d in discarded:
            d.setdefault("query", extra_query)
            discarded_out.append(d)
    logger.info(
        f"historical_archive: {len(passed)}/{len(raw)} relevant candidates for {person!r}"
    )
    return passed[:limit]


def _persist_relevance_reminders(
    task_id: str,
    person: str,
    discarded: list[dict],
    *,
    missing_shots: list[dict] | None = None,
) -> None:
    """把相关性门槛丢弃项与缺素材镜头写入任务清单，供确认屏逐条列出。

    去重后只保留有代表性的条目（同一素材不重复提醒）；丢弃与缺素材都为空
    时不写。写入失败只记日志，绝不阻断生成（辅助诊断信息）。
    """
    missing_shots = missing_shots or []
    if not discarded and not missing_shots:
        return
    try:
        from app.services import task_artifacts

        updates: dict[str, Any] = {}
        if discarded:
            uniq: dict[str, dict] = {}
            for d in discarded:
                key = d.get("url") or d.get("title") or id(d)
                uniq.setdefault(str(key), d)
            updates["material_relevance_reminders"] = {
                "person": person,
                "discarded_count": len(uniq),
                "discarded": list(uniq.values()),
            }
        if missing_shots:
            updates["material_shortage_reminders"] = {
                "person": person,
                "missing_count": len(missing_shots),
                "missing_shots": missing_shots,
                "note": "以下镜头无相关且未排除的档案图，将按任务参数兜底（b-roll 高亮原因）",
            }
        if updates:
            task_artifacts.patch_script_data(task_id, **updates)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"failed to persist relevance reminders: task_id={task_id}, "
            f"error={type(exc).__name__}: {exc}"
        )


def ai_illustration_enabled() -> bool:
    """AI 插图兜底层级是否可用（批次 B）。仅当配置了文生图提供方才启用。"""
    try:
        from app.services import material as _m

        return bool(_m.is_openai_image_enabled())
    except Exception:  # noqa: BLE001
        return False


def _ai_fill_shots(
    *,
    task_id: str,
    person: str,
    shot_terms: list[str],
    video_aspect: VideoAspect,
    max_clip_duration: int,
    needed_duration: float,
    material_directory: str,
) -> tuple[list[str], list[dict[str, Any]]]:
    """为档案未覆盖的镜头生成“AI 插图”片段（批次 B 兜底第二层）。

    层级：相关档案→本函数（AI 插图）→主题性 b-roll（调用方）→仍缺暂停。
    仅在 ``ai_illustration_enabled()`` 为真时工作；绝不抛出（付费接口异常
    降级为空，由调用方继续走 b-roll）。成功片段标 ``ai_illustration=True``，
    供确认屏归为“兜底”、合规与成片 AI 标识使用。
    """
    if needed_duration <= 0 or not ai_illustration_enabled():
        return [], []
    from app.services import material as material_service

    scene_terms = scene_fallback_terms(person, shot_terms, limit=max(1, len(shot_terms)))
    paths: list[str] = []
    records: list[dict[str, Any]] = []
    total = 0.0
    for term in scene_terms:
        if total >= needed_duration:
            break
        try:
            items = material_service.generate_images_openai(
                search_term=term,
                minimum_duration=max_clip_duration,
                video_aspect=video_aspect,
                save_dir=material_directory,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"ai illustration failed for term {term!r}: {type(exc).__name__}"
            )
            continue
        for item in items:
            clip = video_service.render_image_zoom_video(item.url, max_clip_duration)
            if not clip:
                continue
            info = dict(item.source_info or {})
            info["visual_type"] = "ai_illustration"
            info["ai_illustration"] = True
            info["fallback_reason"] = "AI 插图（未找到相关档案素材）"
            try:
                rec = material_service._material_source_record(
                    MaterialInfo(
                        provider=item.provider,
                        url=item.url,
                        duration=max_clip_duration,
                        source_info=info,
                    ),
                    clip,
                )
            except Exception:  # noqa: BLE001
                rec = {"provider": item.provider, "local_file": os.path.basename(clip)}
            rec["ai_illustration"] = True
            paths.append(clip)
            records.append(rec)
            total += max_clip_duration
            break
    if paths:
        logger.info(
            f"historical_archive: AI illustration filled {len(paths)} shot(s) for {person!r}"
        )
    return paths, records


def download_person_materials(
    *,
    task_id: str,
    person: str,
    search_terms: list[str],
    shot_visual_types: list[str] | None,
    video_aspect: VideoAspect,
    audio_duration: float,
    max_clip_duration: int,
    material_directory: str,
) -> list[str]:
    """Download + render archive stills, one per shot, until the duration is met.

    Returns local mp4 clip paths (same contract as other providers) and
    persists license/provenance records via material._persist_material_sources.
    """
    from app.services import material as material_service

    if not enabled():
        logger.warning("historical_archive disabled, skipping archive material fetch")
        return []
    if not material_directory:
        material_directory = utils.task_dir(task_id)

    try:
        required = float(audio_duration)
    except (TypeError, ValueError):
        required = 0.0
    if required <= 0:
        logger.warning(
            "historical_archive: skip, required audio duration is not positive"
        )
        return []

    # Cache the person's image pool once; shots draw from it in order.
    # 人物级黑名单（person_assets.state='excluded'）在检索与选用两侧都强制跳过。
    excluded_keys: set[str] = set()
    try:
        from app.services import person_repo

        excluded_keys = person_repo.excluded_asset_keys(person)
    except Exception as exc:  # noqa: BLE001 — 黑名单读取失败退化为不屏蔽
        logger.warning(
            f"historical_archive: excluded keys lookup failed for {person!r}: "
            f"{type(exc).__name__}: {exc}"
        )

    discarded: list[dict] = []
    pool = find_person_images(
        person,
        limit=max(len(search_terms) * 3, 9),
        discarded_out=discarded,
        excluded_keys=excluded_keys,
    )
    if not pool:
        logger.warning(f"historical_archive: no relevant licensed images for {person!r}")
        missing_all = [
            {"shot_index": i, "query": t, "reason": "无相关且未排除的档案图"}
            for i, t in enumerate(search_terms)
            if not (shot_visual_types and i < len(shot_visual_types)
                    and (shot_visual_types[i] or "").strip() == "b_roll")
        ]
        # 相关档案为空 → 先试 AI 插图（批次 B 兜底第二层），仍无才回退 b-roll。
        ai_paths, ai_records = _ai_fill_shots(
            task_id=task_id, person=person,
            shot_terms=[m["query"] for m in missing_all] or list(search_terms),
            video_aspect=video_aspect, max_clip_duration=max_clip_duration,
            needed_duration=required, material_directory=material_directory,
        )
        if ai_paths:
            material_service._persist_material_sources(task_id, ai_records)
            _persist_relevance_reminders(
                task_id, person, discarded, missing_shots=missing_all
            )
            return ai_paths
        _persist_relevance_reminders(
            task_id, person, discarded, missing_shots=missing_all
        )
        return []

    video_paths: list[str] = []
    material_sources: list[dict[str, Any]] = []
    total_duration = 0.0
    used_urls: set[str] = set()
    used_ids: set[str] = set()
    missing_shots: list[dict] = []
    pool_index = 0
    # 素材池小时允许有限复用（批次 B）：同一素材重复不超过 max_reuse 次，
    # 且每次用不同的 Ken Burns 方向。use_count 按素材身份计次；reusable 按
    # 选用顺序缓存可被复用的素材。
    max_reuse = max(1, int(config.app.get("material_max_reuse", 2) or 2))
    use_count: dict[str, int] = {}
    reusable: list[MaterialInfo] = []

    def _already_used(item: MaterialInfo) -> bool:
        if item.url in used_urls:
            return True
        ident = _asset_identity(item)
        return bool(ident) and ident in used_ids

    for idx, term in enumerate(search_terms):
        visual_type = "historical_photo"
        if shot_visual_types and idx < len(shot_visual_types):
            visual_type = (shot_visual_types[idx] or "historical_photo").strip()

        # Only historical_photo shots (and generated_scene, which currently
        # falls back to archive stills) draw from the archive. b_roll is left
        # to the stock providers by the caller.
        if visual_type == "b_roll":
            continue

        item = None
        direction = ""
        # Prefer a shot-specific search so shots look different from each other.
        # 逐镜独立检索 + 近重复去重：跳过已用/黑名单/同身份素材。
        for candidate in find_person_images(
            person, extra_query=term, limit=3, excluded_keys=excluded_keys
        ):
            if not _already_used(candidate):
                item = candidate
                break
        if item is None:
            while pool_index < len(pool):
                candidate = pool[pool_index]
                pool_index += 1
                if not _already_used(candidate):
                    item = candidate
                    break
        if item is None:
            # 池已用尽：按复用上限选一个重复次数最少且未达上限的素材，
            # 用不同 Ken Burns 方向重渲染，避免画面完全重复。
            best = None
            best_count = None
            for cand in reusable:
                cid = _asset_identity(cand) or cand.url
                cnt = use_count.get(cid, 0)
                if cnt < max_reuse and (best_count is None or cnt < best_count):
                    best, best_count = cand, cnt
            if best is not None:
                item = best
                bid = _asset_identity(best) or best.url
                direction = _REUSE_DIRECTIONS[best_count % len(_REUSE_DIRECTIONS)]
                use_count[bid] = best_count + 1
                logger.info(
                    f"historical_archive: shot {idx} reuses asset "
                    f"({best_count + 1}/{max_reuse}, {direction})"
                )
        if item is None:
            # 素材不足且已达复用上限：登记缺素材镜头，由确认屏列出，
            # 调用方按任务参数决定兜底（暂停/换词/b-roll/AI 图）。
            logger.warning(
                f"historical_archive: no unused image left for shot {idx} ({term!r})"
            )
            missing_shots.append(
                {"shot_index": idx, "query": term, "reason": "独立相关素材不足"}
            )
            continue

        ident = _asset_identity(item)
        first_use = item.url not in used_urls and not (ident and ident in used_ids)
        used_urls.add(item.url)
        if ident:
            used_ids.add(ident)
        if first_use:
            use_count.setdefault(ident or item.url, 1)
            reusable.append(item)
        try:
            image_path = _download_image(item.url, material_directory)
            if not image_path:
                continue
            clip_path = video_service.render_image_zoom_video(
                image_path, max_clip_duration, direction
            )
            if not clip_path:
                continue
            video_paths.append(clip_path)
            info = dict(item.source_info or {})
            info["visual_type"] = visual_type
            # 古代人物素材分类（画像/雕塑/绘画/地图/文物/遗址），供界面标注。
            info["archive_subtype"] = classify_archive_subtype(item)
            if direction:
                info["kenburns_direction"] = direction
            record = material_service._material_source_record(
                MaterialInfo(
                    provider=item.provider,
                    url=item.url,
                    duration=max_clip_duration,
                    source_info=info,
                ),
                clip_path,
            )
            # 记录原始图片文件名（与渲染出的 clip 同目录），供素材登记把真实
            # 历史照片而非临时渲染片段写入人物图片素材库。
            record["image_file"] = os.path.basename(image_path)
            material_sources.append(record)
            total_duration += max_clip_duration
            logger.info(
                f"historical_archive: shot {idx} ({visual_type}) -> "
                f"{os.path.basename(clip_path)}"
            )
            if total_duration > required:
                break
        except Exception as exc:
            logger.error(
                f"historical_archive: shot {idx} failed: {type(exc).__name__}: {exc}"
            )

    logger.success(f"historical_archive: {len(video_paths)} clips rendered")
    # 档案未凑够配音时长 → AI 插图为缺素材镜头兜底（批次 B，b-roll 之前）。
    if total_duration < required:
        ai_paths, ai_records = _ai_fill_shots(
            task_id=task_id, person=person,
            shot_terms=[m["query"] for m in missing_shots] or list(search_terms),
            video_aspect=video_aspect, max_clip_duration=max_clip_duration,
            needed_duration=required - total_duration,
            material_directory=material_directory,
        )
        if ai_paths:
            video_paths.extend(ai_paths)
            material_sources.extend(ai_records)
            logger.info(
                f"historical_archive: AI illustration added {len(ai_paths)} clip(s)"
            )
    if material_sources:
        material_service._persist_material_sources(task_id, material_sources)
    _persist_relevance_reminders(task_id, person, discarded, missing_shots=missing_shots)
    return video_paths


def person_grounding(name: str) -> dict[str, Any]:
    """Facts for the script prompt: local library + Wikipedia summary."""
    out: dict[str, Any] = {"person": name}
    try:
        from app.services import library

        block = library.grounding_block(name)
        if block:
            out["library_block"] = block
    except Exception:
        pass
    summary = fetch_person_summary(name, "zh") or fetch_person_summary(name, "en")
    if summary:
        out["wikipedia_extract"] = summary.get("extract", "")
        out["wikipedia_title"] = summary.get("title", "")
        out["wikipedia_description"] = summary.get("description", "")
        if not out.get("library_block") and summary.get("original_image"):
            out["portrait_url"] = summary["original_image"]
    return out
