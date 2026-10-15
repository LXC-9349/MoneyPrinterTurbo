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


def _download_image(url: str, save_dir: str) -> str:
    """Download one archive still to disk (material.save_image)."""
    from app.services import material

    return material.save_image(url, save_dir=save_dir)


def find_person_images(
    person: str,
    *,
    extra_query: str = "",
    limit: int = 6,
) -> list[MaterialInfo]:
    """Candidate archive images for a person (no downloads).

    ``extra_query`` narrows by scene (e.g. "portrait", "laboratory"), which
    keeps shots visually distinct instead of repeating one headshot.
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

    seen: set[str] = set()
    results: list[MaterialInfo] = []
    for query in queries:
        for item in search_wikimedia_images(query, limit=limit):
            if item.url in seen:
                continue
            seen.add(item.url)
            results.append(item)
        if len(results) >= limit:
            break
    # Supplementary source: LOC has strong US/European photographic archives.
    if len(results) < limit:
        for item in search_loc_images(en_title, limit=limit - len(results)):
            if item.url in seen:
                continue
            seen.add(item.url)
            results.append(item)
        if len(results) < limit:
            for item in search_loc_images(person, limit=limit - len(results)):
                if item.url in seen:
                    continue
                seen.add(item.url)
                results.append(item)
    logger.info(f"historical_archive: {len(results)} candidates for {person!r}")
    return results[:limit]


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
    pool = find_person_images(person, limit=max(len(search_terms) * 3, 9))
    if not pool:
        logger.warning(f"historical_archive: no licensed images for {person!r}")
        return []

    video_paths: list[str] = []
    material_sources: list[dict[str, Any]] = []
    total_duration = 0.0
    used_urls: set[str] = set()
    pool_index = 0

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
        for candidate in find_person_images(person, extra_query=term, limit=2):
            if candidate.url not in used_urls:
                item = candidate
                break
        if item is None:
            while pool_index < len(pool):
                candidate = pool[pool_index]
                pool_index += 1
                if candidate.url not in used_urls:
                    item = candidate
                    break
        if item is None:
            logger.warning(
                f"historical_archive: no unused image left for shot {idx} ({term!r})"
            )
            continue

        used_urls.add(item.url)
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
