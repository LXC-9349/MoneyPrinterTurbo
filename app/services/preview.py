"""M4#4 效果预览服务：分镜预览（秒级、不渲染）+ 试听参数准备 + 参数哈希缓存。

三档预览中"分镜预览"是最有价值、可离线快速返回的一档：逐镜给出候选缩略图、
相关性、来源与授权，顶部给出覆盖率与独立素材数，缺素材/低相关给出醒目警告。

设计约束：
* 只读、绝不下载/渲染，命中缓存直接返回；按参数哈希缓存到磁盘（按内容失效）。
* 单个检索源失败（限流/网络）降级为该镜空候选，不中断整轮预览。
* 相关性口径与 M4#2 relevance_gate、M4#1 评测脚本完全一致，避免"预览好但生成差"。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any

from loguru import logger

from app.config import config
from app.models.schema import MaterialInfo
from app.services.archives import historical_archive as archive
from app.services.relevance_gate import THRESHOLDS, score_relevance
from app.utils import utils

# 预览缓存有效期（秒）：过期即视为"已过期"，强制重算。
CACHE_TTL_SEC = int(config.app.get("preview_cache_ttl", 6 * 3600))


def params_hash(payload: Any) -> str:
    """对任意可 JSON 化参数求稳定哈希（键排序），作为缓存键。"""
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _cache_dir() -> str:
    d = os.path.join(utils.storage_dir(), "cache_preview")
    os.makedirs(d, exist_ok=True)
    return d


def _cache_path(key: str) -> str:
    return os.path.join(_cache_dir(), f"preview-{key}.json")


def cache_get(key: str) -> dict | None:
    """读取未过期的缓存；过期/损坏返回 None（调用方重算）。"""
    path = _cache_path(key)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        if time.time() - float(data.get("ts") or 0) > CACHE_TTL_SEC:
            return None
        return data.get("value")
    except (OSError, ValueError):
        return None


def cache_put(key: str, value: dict) -> None:
    try:
        with open(_cache_path(key), "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "value": value}, f, ensure_ascii=False)
    except OSError as exc:  # pragma: no cover - 缓存写失败降级为不缓存
        logger.warning(f"preview cache write failed: {type(exc).__name__}: {exc}")


def _candidate_view(item: MaterialInfo, ctx: dict, threshold: str, excluded: set[str]) -> dict:
    si = item.source_info or {}
    provider = str(si.get("provider") or item.provider or "")
    score, reason = score_relevance(
        item,
        person_name=ctx["name"],
        aliases=ctx["aliases"],
        era_keywords=ctx["era_keywords"],
        provider=provider,
    )
    cutoff = THRESHOLDS.get(threshold, THRESHOLDS["standard"])
    return {
        "provider": provider,
        "title": str(si.get("asset_id") or "")[:120],
        "author": str((si.get("creator") or {}).get("name") or "")[:80],
        "thumbnail": item.url or "",
        "identity": archive._asset_identity(item) or (item.url or ""),
        "source_page": str(si.get("source_page") or ""),
        "license": str(si.get("license") or ""),
        "license_url": str(si.get("license_url") or ""),
        "relevance": round(score, 2),
        "reason": reason,
        "passes_gate": score >= cutoff,
        "excluded": archive._is_excluded(item, excluded),
    }


def _preview_one_shot(person: str, shot: dict, ctx: dict, threshold: str, excluded: set[str]) -> dict:
    query = str(shot.get("visual_query") or shot.get("query") or "").strip()
    narration = str(shot.get("narration") or "").strip()
    visual_type = str(shot.get("visual_type") or "historical_photo").strip()

    raw: list[MaterialInfo] = []
    seen_urls: set[str] = set()
    seen_ids: set[str] = set()
    # b_roll 镜交由库存源，不在此做档案预览（仍标注以便 UI 区分）。
    if query and visual_type != "b_roll":
        for item in archive.find_person_images(
            person, extra_query=query, limit=8, threshold="off", excluded_keys=excluded
        ):
            if item.url in seen_urls:
                continue
            ident = archive._asset_identity(item)
            if ident and ident in seen_ids:
                continue
            seen_urls.add(item.url)
            if ident:
                seen_ids.add(ident)
            raw.append(item)

    candidates = [_candidate_view(it, ctx, threshold, excluded) for it in raw]
    # 通过门槛的排在前面，其次按相关性降序。
    candidates.sort(key=lambda c: (not c["passes_gate"], -c["relevance"]))
    passing = [c for c in candidates if c["passes_gate"] and not c["excluded"]]

    warning = ""
    # 三数口径（与确认屏一致）：相关=有通过门槛的档案候选；兜底=b_roll 主题镜；缺失=无通过候选。
    if visual_type == "b_roll":
        shot_class = "fallback"
        fallback_reason = "主题性 b-roll（无档案人物素材）"
        warning = ""  # 库存 b-roll 镜，无档案候选属正常
    elif passing:
        shot_class = "relevant"
        fallback_reason = ""
    else:
        shot_class = "missing"
        fallback_reason = ""
        warning = "缺素材：无通过相关性门槛的档案图" if candidates else "缺素材：档案无命中"
    return {
        "narration": narration,
        "query": query,
        "visual_type": visual_type,
        "candidates": candidates[:8],
        "relevant_count": len(passing),
        "independent_assets": len({c["identity"] for c in passing}),
        "shot_class": shot_class,
        "fallback_reason": fallback_reason,
        "warning": warning,
    }


def storyboard_preview(
    person: str,
    shots: list[dict],
    *,
    threshold: str | None = None,
    use_cache: bool = True,
) -> dict:
    """逐镜分镜预览：秒级返回候选与相关性，不下载/渲染，按参数哈希缓存。"""
    person = (person or "").strip()
    th = (threshold or archive._relevance_threshold() or "standard").lower()
    payload = {"person": person, "shots": shots, "threshold": th}
    key = params_hash(payload)
    if use_cache:
        cached = cache_get(key)
        if cached is not None:
            cached["cache"] = {"key": key, "hit": True}
            return cached

    ctx = archive.person_gate_context(person)
    try:
        from app.services import person_repo

        excluded = person_repo.excluded_asset_keys(person)
    except Exception:  # noqa: BLE001
        excluded = set()

    shot_views = [_preview_one_shot(person, s, ctx, th, excluded) for s in (shots or [])]
    total = len(shot_views)
    covered = sum(1 for s in shot_views if s["relevant_count"] > 0)
    archive_shots = [s for s in shot_views if s["visual_type"] != "b_roll"]
    # 三数指标（批次 B）：相关 N / 兜底 K / 缺失 L。兜底不计入“素材齐备”。
    relevant_shots = sum(1 for s in shot_views if s["shot_class"] == "relevant")
    fallback_shots = sum(1 for s in shot_views if s["shot_class"] == "fallback")
    missing_shots_n = sum(1 for s in shot_views if s["shot_class"] == "missing")
    missing = [
        {"shot_index": i, "query": s["query"], "warning": s["warning"],
         "shot_class": s["shot_class"], "fallback_reason": s["fallback_reason"]}
        for i, s in enumerate(shot_views)
        if s["warning"]
    ]
    independent_total = len(
        {c["identity"] for s in shot_views for c in s["candidates"] if c["passes_gate"]}
    )
    result = {
        "person": person,
        "threshold": th,
        "shot_count": total,
        "coverage": round(covered / total, 3) if total else 0.0,
        "independent_assets": independent_total,
        "relevant_shots": relevant_shots,
        "fallback_shots": fallback_shots,
        "missing_shots_count": missing_shots_n,
        "relevant_assets": independent_total,
        "archive_shot_count": len(archive_shots),
        "missing_shots": missing,
        "shots": shot_views,
        "stale": False,
        "cache": {"key": key, "hit": False},
    }
    if use_cache:
        cache_put(key, result)
    return result


# 每秒大致字数（中文、rate=1.0），用于把旁白裁到试听时长。
_CHARS_PER_SEC = 4.5
_SENT_SPLIT = re.compile(r"([\u3002\uff01\uff1f!?.\n]+)")

# 确定性派生预览分镜用的段落/句读分隔（与 task._deterministic_terms 口径一致）。
_SHOT_SEG_SPLIT = re.compile(r"\n\s*\n|[\u3002\uff01\uff1f.!?;\n]+")


def _shot_from_segment(text: str) -> dict:
    """把一段旁白转为预览镜：检索词取去标点后片段关键词。"""
    seg = re.sub(r"\s+", " ", (text or "")).strip()
    query = re.sub(r"[\uff0c,\u3001\uff1a:；;\s]+", " ", seg).strip()
    return {
        "narration": seg[:120],
        "visual_query": query[:24],
        "visual_type": "historical_photo",
    }


def derive_preview_shots(person: str, video_script: str = "", *, limit: int = 6) -> list[dict]:
    """无结构化分镜时的确定性预览分镜（绝不依赖 LLM）。

    有脚本 → 按段落/句读切成逐镜旁白，检索词取片段关键词；
    无脚本 → 以人名 + 档案时代/别名关键词生成主题性场景镜，
    保证创建弹窗在未生成脚本时也能秒级预览取材覆盖情况。
    """
    person = (person or "").strip()
    script = (video_script or "").strip()
    if script:
        segs = [s for s in _SHOT_SEG_SPLIT.split(script) if s and s.strip()]
        return [_shot_from_segment(s) for s in segs[:limit]]
    shots: list[dict] = [{"narration": "", "visual_query": person, "visual_type": "historical_photo"}]
    try:
        ctx = archive.person_gate_context(person)
    except Exception:  # noqa: BLE001 - 无档案上下文时仅留人物名一镜
        ctx = {}
    for kw in (ctx.get("era_keywords") or [])[: max(0, limit - 1)]:
        kw = str(kw).strip()
        if kw:
            shots.append(
                {
                    "narration": "",
                    "visual_query": f"{person} {kw}".strip(),
                    "visual_type": "historical_photo",
                }
            )
    return shots[:limit]


def truncate_for_audition(
    narration: str, *, voice_rate: float = 1.0, max_seconds: int = 15
) -> str:
    """把旁白裁到约 ``max_seconds`` 秒（按语速折算字数），尽量停在句末。

    供“试听”档使用：前端拿到裁后的文本后调用现有 /tts/preview，用所选音色
    与语速合成前段。不追求绝对秒数精确，只要“前约 15 秒”即可。
    """
    text = (narration or "").strip()
    if not text:
        return ""
    rate = max(0.5, float(voice_rate or 1.0))
    target_chars = max(1, int(max_seconds * _CHARS_PER_SEC * rate))
    if len(text) <= target_chars:
        return text
    head = text[:target_chars]
    # 尽量多带一句完整尾句：从截断点往后找最近的句末标点，不超一个短窗。
    tail = text[target_chars : target_chars + 20]
    m = _SENT_SPLIT.search(tail)
    if m:
        return head + tail[: m.end()]
    return head
