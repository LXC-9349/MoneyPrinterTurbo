"""Wikipedia REST summary: factual grounding + portrait for the cover.

  GET https://{lang}.wikipedia.org/api/rest_v1/page/summary/{title}

No key required. Two uses:
  1. ``fetch_person_summary``  — extract + description to ground the script
     and to run the light fact check (phase 3).
  2. ``resolve_english_title`` — map a Chinese name to its English article so
     Commons searches use terms with far better coverage.

Never raises: returns {} / "" on any failure.
"""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import requests
from loguru import logger

from app.config import config

SUMMARY_URL = "https://{lang}.wikipedia.org/api/rest_v1/page/summary/{title}"


def _user_agent() -> str:
    return (
        config.app.get("wikimedia_user_agent", "")
        or os.environ.get("WIKIMEDIA_USER_AGENT", "")
        or "MPT-Biography/0.1 (https://github.com/harry0703/MoneyPrinterTurbo)"
    )


def _get(lang: str, title: str) -> dict[str, Any]:
    url = SUMMARY_URL.format(lang=lang, title=quote(title.replace(" ", "_"), safe=""))
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": _user_agent(), "Accept": "application/json"},
            proxies=config.proxy,
            timeout=(15, 30),
        )
        if resp.status_code != 200:
            return {}
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning(
            f"wikipedia summary failed: lang={lang}, title={title!r}, "
            f"error={type(exc).__name__}"
        )
        return {}


def fetch_person_summary(name: str, lang: str = "zh") -> dict[str, Any]:
    """Summary for a person: {title, extract, description, thumbnail, url, lang}."""
    if not name or not name.strip():
        return {}
    data = _get(lang, name.strip())
    if not data or data.get("type", "").endswith("not_found"):
        return {}
    thumb = data.get("thumbnail") or {}
    original = data.get("originalimage") or {}
    return {
        "title": data.get("title", name),
        "extract": data.get("extract", ""),
        "description": data.get("description", ""),
        "thumbnail": thumb.get("source", ""),
        "original_image": original.get("source", ""),
        "page_url": ((data.get("content_urls") or {}).get("desktop") or {}).get("page", ""),
        "wikibase_item": data.get("wikibase_item", ""),
        "lang": lang,
    }


def resolve_english_title(name: str) -> str:
    """Best-effort English article title for a (possibly Chinese) person name.

    Tries the zh summary's langlinks first (authoritative), then the English
    summary directly (works when the name is already English, e.g. Einstein).
    """
    if not name or not name.strip():
        return ""
    name = name.strip()

    # Already-English names resolve directly.
    if name.isascii():
        data = _get("en", name)
        if data and not data.get("type", "").endswith("not_found"):
            return data.get("title", "") or name
        return name

    zh = fetch_person_summary(name, "zh")
    if zh.get("title"):
        # zh.wikipedia often titles the article with the Western name in
        # parentheses, e.g. "阿尔伯特·爱因斯坦"; query en via langlinks.
        try:
            resp = requests.get(
                "https://zh.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "titles": zh["title"],
                    "prop": "langlinks",
                    "lllang": "en",
                    "format": "json",
                    "redirects": 1,
                },
                headers={"User-Agent": _user_agent()},
                proxies=config.proxy,
                timeout=(15, 30),
            )
            pages = ((resp.json().get("query") or {}).get("pages") or {})
            for page in pages.values():
                links = page.get("langlinks") or []
                if links:
                    return links[0].get("*", "") or ""
        except Exception as exc:
            logger.warning(f"wikipedia langlink lookup failed: {type(exc).__name__}")
    return ""


def search_commons_via_wikidata(name: str) -> list[str]:
    """Deprecated placeholder kept out of the hot path (see archives/__init__).

    Kept intentionally empty so future Wikidata-based image lookup has a named
    seam without silently adding another network dependency.
    """
    return []
