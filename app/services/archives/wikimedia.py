"""Wikimedia Commons image search with license gating.

Uses the MediaWiki API (no key required):
  action=query&generator=search&gsrnamespace=6 (File:)&prop=imageinfo
  iiprop=url|size|extmetadata  -> LicenseShortName / Artist / Credit

Only images whose license is public-domain / CC0 / CC BY (not ShareAlike) are
returned, because biography videos are published commercially on Douyin/B站/
YouTube and ShareAlike would force relicensing the entire video.
"""
from __future__ import annotations

import os
import re
from urllib.parse import urlencode

import requests
from loguru import logger

from app.config import config
from app.models.schema import MaterialInfo

COMMONS_API = "https://commons.wikimedia.org/w/api.php"

# License short names that are safe for commercial reuse without viral terms.
_ALLOWED_LICENSE_PATTERNS = (
    r"\bpublic domain\b",
    r"\bpd[- ]",
    r"\bcc0\b",
    r"\bcc[- ]?by[- ]?[234]\.0\b",
    r"\bcc[- ]?by\b",  # generic CC BY (not SA)
)
# Explicitly rejected even when they also contain "cc-by": ShareAlike and
# NonCommercial/NoDerivatives cannot ship inside a monetised video.
_REJECTED_LICENSE_PATTERNS = (
    r"sa\b",
    r"share[- ]?alike",
    r"\bnc\b",
    r"non[- ]?commercial",
    r"\bnd\b",
    r"no[- ]?deriv",
    r"fair use",
    r"copyright",
    r"all rights reserved",
)

# Commons search matches any file whose text mentions the name, including PDFs,
# maps and photos of streets/schools *named after* the person. Only actual
# still images are usable as biography footage.
_IMAGE_EXT_RE = r"\.(jpe?g|png|gif|tiff?|webp)$"
# Filenames indicating the subject is something named after the person
# (street, school, memorial, building) rather than the person themselves.
_SUBJECT_NOISE_HINTS = (
    "straße", "strasse", "street", "gymnasium", "school", "schule",
    "memorial", "denkmal", "monument", "statue", "plaque", "targa",
    "gebäude", "building", "brücke", "bridge", "platz", "square",
    "universität", "university", "institut", "institute",
    "museum", "bibliothek", "library", "station", "bahnhof",
    "stadium", "park", "tunnel", "avenue", "road", "observatory",
)


def _is_noise_asset(asset_id: str) -> bool:
    """Reject Commons files that are not plausible footage of the subject."""
    name = (asset_id or "").lower()
    if not name:
        return False
    if not re.search(_IMAGE_EXT_RE, name):
        return True  # PDF/DjVu/SVG/map files are not video material
    return any(hint in name for hint in _SUBJECT_NOISE_HINTS)


def _user_agent() -> str:
    return (
        config.app.get("wikimedia_user_agent", "")
        or os.environ.get("WIKIMEDIA_USER_AGENT", "")
        or "MPT-Biography/0.1 (https://github.com/harry0703/MoneyPrinterTurbo)"
    )


def classify_license(short_name: str, license_name: str = "") -> tuple[bool, str]:
    """Return (allowed, reason) for one license pair."""
    text = f"{short_name} {license_name}".strip().lower()
    if not text:
        return False, "no license metadata"
    for pat in _REJECTED_LICENSE_PATTERNS:
        if re.search(pat, text):
            return False, f"restricted license: {short_name or license_name}"
    for pat in _ALLOWED_LICENSE_PATTERNS:
        if re.search(pat, text):
            return True, short_name or license_name
    return False, f"unrecognised license: {short_name or license_name}"


def _strip_html(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value or "").strip()


def search_images(
    search_term: str,
    minimum_duration: int = 0,
    limit: int = 12,
    min_width: int | None = None,
) -> list[MaterialInfo]:
    """Search Commons for freely-licensed images of ``search_term``.

    Returns MaterialInfo records whose ``duration`` is a placeholder (images
    are rendered to clips later) and whose ``source_info`` carries license and
    provenance for the compliance manifest.
    """
    if not search_term or not search_term.strip():
        return []
    min_width = min_width or int(
        config.app.get("historical_archive_min_image_width", 640) or 640
    )
    params = {
        "action": "query",
        "generator": "search",
        "gsrsearch": search_term,
        "gsrnamespace": 6,  # File: namespace
        "gsrlimit": min(limit * 2, 40),
        "prop": "imageinfo",
        "iiprop": "url|size|extmetadata",
        "iiextmetadatafilter": "LicenseShortName|License|Artist|Credit|ImageDescription",
        "iiurlwidth": 1600,  # pre-scaled rendition, avoids giant originals
        "format": "json",
    }
    url = f"{COMMONS_API}?{urlencode(params)}"
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": _user_agent()},
            proxies=config.proxy,
            timeout=(20, 45),
        )
        if resp.status_code != 200:
            logger.warning(
                f"wikimedia commons search failed: status={resp.status_code}, "
                f"term={search_term!r}"
            )
            return []
        data = resp.json()
    except Exception as exc:
        logger.warning(
            f"wikimedia commons search error: term={search_term!r}, "
            f"error={type(exc).__name__}"
        )
        return []

    pages = ((data.get("query") or {}).get("pages") or {})
    results: list[MaterialInfo] = []
    skipped: list[str] = []
    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        if not info:
            continue
        width = int(info.get("width") or 0)
        if width and width < min_width:
            skipped.append(f"{page.get('title')}: too small ({width}px)")
            continue
        meta = info.get("extmetadata") or {}
        short_name = _strip_html((meta.get("LicenseShortName") or {}).get("value", ""))
        license_name = _strip_html((meta.get("License") or {}).get("value", ""))
        allowed, reason = classify_license(short_name, license_name)
        if not allowed:
            skipped.append(f"{page.get('title')}: {reason}")
            continue
        if _is_noise_asset(page.get("title", "")):
            skipped.append(f"{page.get('title')}: not a usable photo of the subject")
            continue

        # Prefer the 1600px thumbnail; fall back to the original file URL.
        image_url = info.get("thumburl") or info.get("url") or ""
        if not image_url:
            continue
        item = MaterialInfo()
        item.provider = "wikimedia"
        item.url = image_url
        # Images are rendered into clips of video_clip_duration later; 0 here
        # keeps them out of the "found_duration" video-length accounting.
        item.duration = 0
        item.source_info = {
            "provider": "wikimedia",
            "search_term": search_term,
            "asset_id": page.get("title", ""),
            "source_page": info.get("descriptionurl") or "",
            "license": short_name or license_name,
            "license_url": _strip_html((meta.get("License") or {}).get("value", "")),
            "creator": {
                "name": _strip_html((meta.get("Artist") or {}).get("value", ""))[:120],
            },
            "rendition": {
                "width": width,
                "height": int(info.get("height") or 0),
            },
            "credit": _strip_html((meta.get("Credit") or {}).get("value", ""))[:160],
        }
        results.append(item)
        if len(results) >= limit:
            break

    logger.info(
        f"wikimedia: {len(results)} usable / {len(pages)} found for {search_term!r}"
        + (f" | skipped: {skipped[:3]}" if skipped else "")
    )
    return results
