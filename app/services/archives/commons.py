"""Wikimedia Commons structured sourcing for a resolved Wikidata QID (batch B).

The old relevance gate matched the person's *name* against a Commons filename,
which fails for figures whose files are titled in another language or by an
opaque identifier (Cao Cao's archive candidates passed at 0%). This module
sources candidates by *structure* instead, so relevance does not depend on a
filename substring:

  a. ``category_files``   — the person's P373 Commons category, walked to
     depth <= 2, skipping "popular culture"-style subcategories.
  b. ``search_depicts``   — Commons full-text ``haswbstatement:P180=<QID>``:
     files whose structured data explicitly depicts the entity.
  c. ``image_from_filename`` — the P18 image value resolved to a licensed file.

Every returned MaterialInfo carries ``source_info['relevance_basis']``
('category' | 'depicts' | 'wikidata_image') so the gate can accept it without a
name match, plus license/author/source-page metadata for the compliance
sidecar. Key-less APIs; never raise (a dead endpoint returns []).
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any
from urllib.parse import urlencode

import requests
from loguru import logger

from app.config import config
from app.models.schema import MaterialInfo
from app.services.archives import wikimedia

COMMONS_API = "https://commons.wikimedia.org/w/api.php"

# 礼貌限速 + 429/503 退避：Commons/Wikidata 对突发请求返回 429，旧实现把非
# 200 一律当作空结果，导致分类树取材被静默抽干成 0~1 张。这里进程内串行限速，
# 并在被限流时尊重 Retry-After 退避重试，避免把限流误判成“该人物没有素材”。
_MIN_INTERVAL_SEC = float(config.app.get("wikimedia_min_request_interval", 1.0) or 1.0)
_rate_lock = threading.Lock()
_last_request_ts = 0.0
_MAX_RETRIES = 4


def _throttle() -> None:
    global _last_request_ts
    with _rate_lock:
        now = time.monotonic()
        wait = _MIN_INTERVAL_SEC - (now - _last_request_ts)
        if wait > 0:
            time.sleep(wait)
        _last_request_ts = time.monotonic()


def _backoff_seconds(resp, attempt: int) -> float:
    retry_after = resp.headers.get("Retry-After") if resp is not None else None
    try:
        delay = float(retry_after) if retry_after else 1.5 * (attempt + 1)
    except (TypeError, ValueError):
        delay = 1.5 * (attempt + 1)
    return min(max(delay, 1.0), 20.0)

# Subcategory names that collect fan art / memes / modern depictions rather than
# authentic portraits or artefacts. Traversed categories matching these are cut.
_POP_CULTURE_HINTS = (
    "pop culture", "popular culture", "in popular culture", "fan art", "fanart",
    "memes", "cosplay", "fiction", "comics", "video games", "films", "movies",
    "television", "tv series", "lego", "toys", "merchandise", "posters",
    "in art", "cultural references", "流行文化", "电子游戏", "游戏",
    "动漫", "影视", "手办", "玩具", "同人",
)

_IMAGE_EXT_RE = wikimedia._IMAGE_EXT_RE


def _user_agent() -> str:
    return wikimedia._user_agent()


def _get(params: dict[str, Any], timeout: tuple[int, int] = (15, 40)) -> dict[str, Any]:
    url = f"{COMMONS_API}?{urlencode(params)}"
    headers = {"User-Agent": _user_agent(), "Accept": "application/json"}
    for attempt in range(_MAX_RETRIES):
        _throttle()
        try:
            resp = requests.get(
                url, headers=headers, proxies=config.proxy, timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"commons api error: {type(exc).__name__}")
            return {}
        if resp.status_code == 200:
            data = resp.json()
            return data if isinstance(data, dict) else {}
        if resp.status_code in (429, 503):
            delay = _backoff_seconds(resp, attempt)
            logger.warning(
                f"commons rate-limited status={resp.status_code}, backoff {delay:.1f}s "
                f"({attempt + 1}/{_MAX_RETRIES})"
            )
            time.sleep(delay)
            continue
        logger.warning(f"commons api failed: status={resp.status_code}")
        return {}
    logger.warning("commons api: giving up after rate-limit retries")
    return {}


def _is_pop_culture(category: str) -> bool:
    low = (category or "").lower()
    return any(hint in low for hint in _POP_CULTURE_HINTS)


def _category_members(category: str) -> tuple[list[str], list[str]]:
    """Direct members of one Commons category: (file_titles, subcategory_names)."""
    title = category if category.lower().startswith("category:") else f"Category:{category}"
    files: list[str] = []
    subcats: list[str] = []
    cont: dict[str, Any] = {}
    for _ in range(4):  # bounded pagination
        params: dict[str, Any] = {
            "action": "query",
            "list": "categorymembers",
            "cmtitle": title,
            "cmtype": "file|subcat",
            "cmlimit": 200,
            "format": "json",
            **cont,
        }
        data = _get(params)
        for member in (data.get("query") or {}).get("categorymembers") or []:
            mtitle = str(member.get("title") or "")
            ns = int(member.get("ns") or 0)
            if ns == 6 and mtitle:  # File:
                files.append(mtitle)
            elif ns == 14 and mtitle:  # Category:
                subcats.append(mtitle[len("Category:"):])
        cont = (data.get("continue") or {})
        if not cont:
            break
    return files, subcats


def category_files(category: str, *, depth: int = 2, max_files: int = 200) -> list[str]:
    """File titles under a category and its subcategories (depth <= ``depth``).

    Pop-culture subcategories are pruned. Returns at most ``max_files`` titles
    (breadth-first so the top-level, most authoritative files come first).
    """
    category = (category or "").strip()
    if not category:
        return []
    collected: list[str] = []
    seen_titles: set[str] = set()
    frontier = [category]
    for level in range(depth + 1):
        next_frontier: list[str] = []
        for cat in frontier:
            files, subcats = _category_members(cat)
            for f in files:
                if f not in seen_titles:
                    seen_titles.add(f)
                    collected.append(f)
            if level < depth:
                for sub in subcats:
                    if _is_pop_culture(sub):
                        continue
                    if sub not in frontier and sub not in next_frontier:
                        next_frontier.append(sub)
            if len(collected) >= max_files:
                return collected[:max_files]
        frontier = next_frontier
        if not frontier:
            break
    return collected[:max_files]


def search_depicts(qid: str, *, limit: int = 40) -> list[str]:
    """File titles whose structured data depicts ``qid`` (haswbstatement:P180)."""
    qid = (qid or "").strip()
    if not qid.startswith("Q"):
        return []
    data = _get({
        "action": "query",
        "generator": "search",
        "gsrsearch": f"haswbstatement:P180={qid}",
        "gsrnamespace": 6,
        "gsrlimit": min(limit, 50),
        "format": "json",
    })
    pages = ((data.get("query") or {}).get("pages") or {})
    titles: list[str] = []
    for page in pages.values():
        t = str(page.get("title") or "")
        if t.startswith("File:"):
            titles.append(t)
    return titles[:limit]


def _imageinfo_by_titles(titles: list[str], *, iiurlwidth: int = 1600) -> dict[str, dict]:
    """Batch imageinfo for up to 50 File: titles per request. title -> imageinfo."""
    out: dict[str, dict] = {}
    for i in range(0, len(titles), 50):
        chunk = titles[i:i + 50]
        cont: dict[str, Any] = {}
        for _ in range(4):
            params: dict[str, Any] = {
                "action": "query",
                "titles": "|".join(chunk),
                "prop": "imageinfo",
                "iiprop": "url|size|extmetadata",
                "iiextmetadatafilter": "LicenseShortName|License|Artist|Credit|ImageDescription",
                "iiurlwidth": iiurlwidth,
                "format": "json",
                **cont,
            }
            data = _get(params)
            for page in ((data.get("query") or {}).get("pages") or {}).values():
                info = (page.get("imageinfo") or [{}])[0]
                if info:
                    out[str(page.get("title") or "")] = info
            cont = (data.get("continue") or {})
            if not cont:
                break
    return out


def _min_width() -> int:
    return int(config.app.get("historical_archive_min_image_width", 640) or 640)


def _build_material(title: str, info: dict, *, basis: str, qid: str, category: str) -> MaterialInfo | None:
    """One Commons file -> MaterialInfo with license gating (mirrors wikimedia)."""
    width = int(info.get("width") or 0)
    if width and width < _min_width():
        return None
    meta = info.get("extmetadata") or {}
    short_name = wikimedia._strip_html((meta.get("LicenseShortName") or {}).get("value", ""))
    license_name = wikimedia._strip_html((meta.get("License") or {}).get("value", ""))
    allowed, reason = wikimedia.classify_license(short_name, license_name)
    if not allowed:
        return None
    # Structured sourcing already proves relevance, but a PDF/DjVu/street-sign
    # file is still not usable biography footage.
    if wikimedia._is_noise_asset(title):
        return None
    image_url = info.get("thumburl") or info.get("url") or ""
    if not image_url:
        return None
    description = wikimedia._strip_html((meta.get("ImageDescription") or {}).get("value", ""))

    item = MaterialInfo()
    item.provider = "wikimedia"
    item.url = image_url
    item.duration = 0
    item.source_info = {
        "provider": "wikimedia",
        "search_term": qid or category or title,
        "asset_id": title,
        "source_page": info.get("descriptionurl") or "",
        "license": short_name or license_name,
        "license_url": wikimedia._strip_html((meta.get("License") or {}).get("value", "")),
        "creator": {"name": wikimedia._strip_html((meta.get("Artist") or {}).get("value", ""))[:120]},
        "credit": wikimedia._strip_html((meta.get("Credit") or {}).get("value", ""))[:160],
        "description": description[:400],
        "rendition": {"width": width, "height": int(info.get("height") or 0)},
        # Structural relevance proof consumed by the gate (batch B).
        "relevance_basis": basis,
        "depicts_qid": qid,
        "category": category,
    }
    return item


def materials_from_titles(
    titles: list[str], *, basis: str, qid: str = "", category: str = ""
) -> list[MaterialInfo]:
    """Resolve File: titles to licensed MaterialInfo records tagged with basis."""
    titles = [t for t in dict.fromkeys(titles or []) if t]
    if not titles:
        return []
    infos = _imageinfo_by_titles(titles)
    out: list[MaterialInfo] = []
    for title in titles:
        info = infos.get(title)
        if not info:
            continue
        item = _build_material(title, info, basis=basis, qid=qid, category=category)
        if item is not None:
            out.append(item)
    return out


def image_from_filename(filename: str, *, qid: str = "") -> list[MaterialInfo]:
    """Resolve a Wikidata P18 image value (a Commons file name) to material."""
    filename = (filename or "").strip()
    if not filename:
        return []
    title = filename if filename.lower().startswith("file:") else f"File:{filename}"
    # P18 images are the canonical depiction; still license-gated for safety.
    return materials_from_titles([title], basis="wikidata_image", qid=qid)


def collect_qid_materials(qid: str, *, category: str = "", limit: int = 40) -> list[MaterialInfo]:
    """All structurally-relevant Commons material for a QID, deduped by identity.

    Order of authority: P18 image (canonical) -> depicts search -> category
    tree. Deduped by asset identity so the same photo reached two ways counts
    once. Never raises.
    """
    if not qid or not qid.startswith("Q"):
        return []
    from app.services.archives import wikidata as wd

    collected: list[MaterialInfo] = []
    seen: set[str] = set()

    def _add(items: list[MaterialInfo]) -> None:
        for it in items:
            ident = str((it.source_info or {}).get("asset_id") or it.url or "").lower()
            if ident and ident in seen:
                continue
            if ident:
                seen.add(ident)
            collected.append(it)

    try:
        for fname in wd.image_files(qid):
            _add(image_from_filename(fname, qid=qid))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"commons: P18 lookup failed for {qid}: {type(exc).__name__}")

    try:
        depicts_titles = search_depicts(qid, limit=limit)
        _add(materials_from_titles(depicts_titles, basis="depicts", qid=qid))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"commons: depicts lookup failed for {qid}: {type(exc).__name__}")

    cat = category or ""
    if not cat:
        try:
            cat = wd.commons_category(qid)
        except Exception:  # noqa: BLE001
            cat = ""
    if cat:
        try:
            cat_titles = category_files(cat, depth=2, max_files=limit * 3)
            _add(materials_from_titles(cat_titles, basis="category", qid=qid, category=cat))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"commons: category walk failed for {cat!r}: {type(exc).__name__}")

    logger.info(
        f"commons: {len(collected)} structurally-relevant files for {qid} "
        f"(category={cat!r})"
    )
    return collected[: max(limit, 12)]
