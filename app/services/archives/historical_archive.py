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


def _relevance_threshold() -> str:
    """三档相关性门槛（strict/standard/loose），"off"/"" 关闭过滤。

    默认标准档；由 config.app.material_relevance_threshold 覆盖，便于按任务
    参数在确认屏切换宽严。
    """
    th = str(config.app.get("material_relevance_threshold", "standard") or "standard")
    return th.strip().lower()


def person_gate_context(person: str) -> dict:
    """为相关性门槛收集上下文：主名 + 别名 + 英文名 + 时代关键词。

    别名取本地人物库（person_repo.list_aliases）；英文名走 Wikipedia 解析；
    时代关键词取史料库档案的 era_en（逗号分隔则展开）。任一环节失败都不
    抛出，只退化可用字段——门槛本身对缺失字段宽容。
    """
    person = (person or "").strip()
    aliases: list[str] = []
    era_keywords: list[str] = []
    if not person:
        return {"name": person, "aliases": aliases, "era_keywords": era_keywords}

    try:
        from app.services import person_repo

        aliases = [a["alias"] for a in person_repo.list_aliases(person) if a.get("alias")]
    except Exception:  # noqa: BLE001 — 别名库不可用不应阻断检索
        aliases = []

    en = resolve_english_title(person) or ""
    if en and en not in aliases and en != person:
        aliases.append(en)

    try:
        from app.services import library

        era = library.era_keywords(person, "") or ""
        era_keywords = [p.strip() for p in era.split(",") if p.strip()]
    except Exception:  # noqa: BLE001
        era_keywords = []

    return {"name": person, "aliases": aliases, "era_keywords": era_keywords}


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
        _persist_relevance_reminders(
            task_id,
            person,
            discarded,
            missing_shots=[
                {"shot_index": i, "query": t, "reason": "无相关且未排除的档案图"}
                for i, t in enumerate(search_terms)
                if not (shot_visual_types and i < len(shot_visual_types)
                        and (shot_visual_types[i] or "").strip() == "b_roll")
            ],
        )
        return []

    video_paths: list[str] = []
    material_sources: list[dict[str, Any]] = []
    total_duration = 0.0
    used_urls: set[str] = set()
    used_ids: set[str] = set()
    missing_shots: list[dict] = []
    pool_index = 0

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
            # 素材不足不再静默循环复用：登记缺素材镜头，由确认屏列出，
            # 调用方按任务参数决定兜底（暂停/换词/b-roll/AI 图）。
            logger.warning(
                f"historical_archive: no unused image left for shot {idx} ({term!r})"
            )
            missing_shots.append(
                {"shot_index": idx, "query": term, "reason": "独立相关素材不足"}
            )
            continue

        used_urls.add(item.url)
        ident = _asset_identity(item)
        if ident:
            used_ids.add(ident)
        try:
            image_path = _download_image(item.url, material_directory)
            if not image_path:
                continue
            clip_path = video_service.render_image_zoom_video(
                image_path, max_clip_duration
            )
            if not clip_path:
                continue
            video_paths.append(clip_path)
            info = dict(item.source_info or {})
            info["visual_type"] = visual_type
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
