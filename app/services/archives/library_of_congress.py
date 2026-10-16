"""Library of Congress photo search (no API key required).

  GET https://www.loc.gov/photos/?fo=json&q={name}

Used as a supplementary historical-photo source for English-language figures
(US/European archives are strongest there). Records expose ``rights`` text, so
the same commercial-use gate as Commons is applied.
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Any

import requests
from loguru import logger

from app.config import config
from app.models.schema import MaterialInfo

LOC_API = "https://www.loc.gov/photos/"

# LoC 持续 403 修复（批次 B）：裸 UA "MPT-Biography/0.1" 会被 LoC 的 WAF 拒。
# 改用带标识/联系方式的规范 UA，并加最小请求间隔限速（LoC 建议 ≤ 1 req/s）。
_LOC_UA = (
    config.app.get("loc_user_agent", "")
    or os.environ.get("LOC_USER_AGENT", "")
    or "MPT-Biography/0.1 (+https://github.com/harry0703/MoneyPrinterTurbo; contact: user@example.com)"
)
# 进程内串行限速：两次 LoC 请求至少间隔 _MIN_INTERVAL_SEC。
_MIN_INTERVAL_SEC = float(config.app.get("loc_min_interval_sec", 1.2) or 1.2)
_rate_lock = threading.Lock()
_last_request_ts = 0.0
# 供系统检查页读取：最近一次 LoC 是否不可用（连续 403/429）。
_last_status: dict[str, Any] = {"ok": True, "code": 0, "detail": ""}


def _throttle() -> None:
    global _last_request_ts
    with _rate_lock:
        now = time.monotonic()
        wait = _MIN_INTERVAL_SEC - (now - _last_request_ts)
        if wait > 0:
            time.sleep(wait)
        _last_request_ts = time.monotonic()


def availability() -> dict[str, Any]:
    """最近一次 LoC 检索结果，供系统检查页标注红项（不主动发起请求）。"""
    return dict(_last_status)


def _record_status(code: int, detail: str = "") -> None:
    _last_status.update({"ok": code in (0, 200), "code": code, "detail": detail[:200]})

_REJECTED = (
    r"restricted",
    r"all rights reserved",
    r"permission",
    r"copyright not evaluated",
    r"rights status.*unknown",
)

# LOC mixes collection landing pages and icons into /photos results; only real
# digitised still images hosted on tile.loc.gov/storage-services are usable.
_IMAGE_HOST_HINT = "loc.gov/"


def _format_list(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(str(v) for v in value).lower()
    return str(value or "").lower()


def _allowed_rights(rights: str) -> tuple[bool, str]:
    text = (rights or "").lower()
    if not text:
        # LOC's default for most of its digitised collections is no known
        # restrictions; keep it but flag the license string as unknown.
        return True, "no known restrictions (LOC default)"
    for pat in _REJECTED:
        if re.search(pat, text):
            return False, rights
    if re.search(r"no known restrictions|public domain|unrestricted", text):
        return True, rights
    return True, rights


def search_images(
    search_term: str,
    minimum_duration: int = 0,
    limit: int = 8,
) -> list[MaterialInfo]:
    """Search LOC photos; images are later rendered into clips."""
    if not search_term or not search_term.strip():
        return []
    params = {"fo": "json", "q": search_term, "c": min(limit * 2, 40)}
    _throttle()
    try:
        resp = requests.get(
            LOC_API,
            params=params,
            proxies=config.proxy,
            timeout=(20, 45),
            headers={
                "User-Agent": _LOC_UA,
                "Accept": "application/json",
            },
        )
        if resp.status_code != 200:
            _record_status(resp.status_code, f"status={resp.status_code}")
            logger.warning(f"loc.gov search failed: status={resp.status_code}")
            return []
        data = resp.json()
        _record_status(200)
    except Exception as exc:
        _record_status(0, f"{type(exc).__name__}")
        logger.warning(f"loc.gov search error: {type(exc).__name__}")
        return []

    results: list[MaterialInfo] = []
    for entry in (data.get("results") or []):
        # Skip collection landing pages / non-photo formats outright.
        fmt = _format_list(entry.get("original_format"))
        if "collection" in fmt or "photo" not in fmt:
            continue
        image_url = ""
        for key in ("image_url", "thumb_url"):
            value = entry.get(key)
            if isinstance(value, list) and value:
                image_url = value[-1]
            elif isinstance(value, str) and value:
                image_url = value
            if image_url:
                break
        if not image_url or _IMAGE_HOST_HINT not in image_url:
            continue
        # LOC sometimes returns .tif/.gif; only common web formats render.
        if not re.search(r"\.(jpe?g|png|webp)(#|\?|$)", image_url, re.I):
            continue
        rights = ""
        raw_rights = entry.get("rights")
        if isinstance(raw_rights, list) and raw_rights:
            rights = str(raw_rights[0])
        elif isinstance(raw_rights, str):
            rights = raw_rights
        ok, reason = _allowed_rights(rights)
        if not ok:
            continue

        item = MaterialInfo()
        item.provider = "loc"
        item.url = image_url
        item.duration = 0
        item.source_info = {
            "provider": "loc",
            "search_term": search_term,
            "asset_id": str(entry.get("id") or entry.get("url") or ""),
            "source_page": entry.get("url") or entry.get("id") or "",
            "license": reason,
            "license_url": "https://www.loc.gov/legal/",
            "creator": {"name": str(entry.get("contributor") or "")[:120]},
            "credit": "Library of Congress",
        }
        results.append(item)
        if len(results) >= limit:
            break

    logger.info(f"loc.gov: {len(results)} usable images for {search_term!r}")
    return results
