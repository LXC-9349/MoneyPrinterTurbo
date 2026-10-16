"""Wikidata entity resolution for biography material sourcing (batch B).

Person name -> Wikidata QID -> labels/aliases (zh+en) -> structured image
claims (P18 image, P373 Commons category). These feed the Commons sourcing in
``commons.py`` and the relevance gate: a candidate that depicts the resolved
QID, or lives in the person's Commons category tree, is relevant by structure
rather than by a fragile filename substring match.

Key-less public APIs (WBSearchEntities / WBGetEntities). Design rules mirror
the rest of ``app.services.archives``:
  * Never raise: a dead API returns "" / {} / [] so one outage cannot kill a
    task or the confirmation screen.
  * Results are memoised per process so a task that touches the same person
    across shots does not re-query Wikidata.
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

WIKIDATA_API = "https://www.wikidata.org/w/api.php"

# P31 (instance of) value that marks a human; used to disambiguate a name that
# also matches a place/work/ship with a higher raw search score.
_HUMAN_QID = "Q5"

_lock = threading.Lock()
# name(lower) -> qid ; qid -> entity payload. Process-local memo.
_QID_CACHE: dict[str, str] = {}
_ENTITY_CACHE: dict[str, dict[str, Any]] = {}

# 与 commons.py 一致：进程内串行限速 + 429/503 尊重 Retry-After 退避重试，
# 避免限流被静默当成“解析不到 QID”。
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


def _user_agent() -> str:
    return (
        config.app.get("wikimedia_user_agent", "")
        or os.environ.get("WIKIMEDIA_USER_AGENT", "")
        or "MPT-Biography/0.1 (https://github.com/harry0703/MoneyPrinterTurbo)"
    )


def _get(params: dict[str, Any], timeout: tuple[int, int] = (15, 30)) -> dict[str, Any]:
    url = f"{WIKIDATA_API}?{urlencode(params)}"
    headers = {"User-Agent": _user_agent(), "Accept": "application/json"}
    for attempt in range(_MAX_RETRIES):
        _throttle()
        try:
            resp = requests.get(
                url, headers=headers, proxies=config.proxy, timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 — 网络/解析失败一律降级为空
            logger.warning(f"wikidata api error: {type(exc).__name__}")
            return {}
        if resp.status_code == 200:
            data = resp.json()
            return data if isinstance(data, dict) else {}
        if resp.status_code in (429, 503):
            delay = _backoff_seconds(resp, attempt)
            logger.warning(
                f"wikidata rate-limited status={resp.status_code}, backoff {delay:.1f}s "
                f"({attempt + 1}/{_MAX_RETRIES})"
            )
            time.sleep(delay)
            continue
        logger.warning(f"wikidata api failed: status={resp.status_code}")
        return {}
    logger.warning("wikidata api: giving up after rate-limit retries")
    return {}


def _is_human(entity: dict[str, Any]) -> bool:
    """P31 (instance of) contains Q5, or the description hints at a person."""
    claims = entity.get("claims") or {}
    for p31 in claims.get("P31", []) or []:
        val = ((p31.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {}
        if val.get("id") == _HUMAN_QID:
            return True
    return False


def resolve_qid(name: str, *, lang: str = "zh", force: bool = False) -> str:
    """Best-effort Wikidata QID for a person name; "" when unresolved.

    Uses WBSearchEntities and prefers a result that is a human (P31=Q5). The
    lookup is memoised per process. ``force`` bypasses the memo (used after a
    manual correction is written to persons.qid).
    """
    name = (name or "").strip()
    if not name:
        return ""
    key = f"{lang}:{name.lower()}"
    if not force:
        with _lock:
            cached = _QID_CACHE.get(key)
        if cached is not None:
            return cached

    data = _get({
        "action": "wbsearchentities",
        "search": name,
        "language": lang,
        "uselang": lang,
        "type": "item",
        "limit": 8,
        "format": "json",
    })
    qid = ""
    fallback_qid = ""
    for hit in (data.get("search") or []):
        hid = str(hit.get("id") or "")
        if not hid.startswith("Q"):
            continue
        if not fallback_qid:
            fallback_qid = hid
        ent = entity_data(hid, props="claims")
        if _is_human(ent):
            qid = hid
            break
    qid = qid or fallback_qid
    with _lock:
        _QID_CACHE[key] = qid
    if qid:
        logger.info(f"wikidata: resolved {name!r} ({lang}) -> {qid}")
    else:
        logger.info(f"wikidata: no QID for {name!r} ({lang})")
    return qid


def entity_data(qid: str, *, props: str = "labels|aliases|claims") -> dict[str, Any]:
    """WBGetEntities payload for one QID (memoised by qid+props)."""
    qid = (qid or "").strip()
    if not qid.startswith("Q"):
        return {}
    cache_key = f"{qid}:{props}"
    with _lock:
        cached = _ENTITY_CACHE.get(cache_key)
    if cached is not None:
        return cached
    params: dict[str, Any] = {
        "action": "wbgetentities",
        "ids": qid,
        "props": props,
        "format": "json",
    }
    if "labels" in props or "aliases" in props:
        params["languages"] = "zh|en"
    data = _get(params)
    entity = ((data.get("entities") or {}).get(qid)) or {}
    with _lock:
        _ENTITY_CACHE[cache_key] = entity
    return entity


def labels_and_aliases(qid: str) -> list[str]:
    """zh+en labels and aliases for a QID — the alias set used by the gate."""
    entity = entity_data(qid, props="labels|aliases")
    if not entity:
        return []
    out: list[str] = []
    for group in ("labels", "aliases"):
        for _lang, entries in (entity.get(group) or {}).items():
            if group == "labels":
                value = (entries or {}).get("value")
                if value:
                    out.append(str(value))
            else:
                for entry in entries or []:
                    value = (entry or {}).get("value")
                    if value:
                        out.append(str(value))
    # Dedupe, keep order, drop empties.
    return list(dict.fromkeys(v.strip() for v in out if v and v.strip()))


def _claim_qid_values(claims: dict[str, Any], prop: str) -> list[str]:
    out: list[str] = []
    for claim in claims.get(prop, []) or []:
        val = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {}
        vid = val.get("id")
        if isinstance(vid, str) and vid.startswith("Q"):
            out.append(vid)
    return out


def _claim_string_values(claims: dict[str, Any], prop: str) -> list[str]:
    out: list[str] = []
    for claim in claims.get(prop, []) or []:
        snak = claim.get("mainsnak") or {}
        val = (snak.get("datavalue") or {}).get("value")
        if isinstance(val, str) and val.strip():
            out.append(val.strip())
    return out


def image_files(qid: str) -> list[str]:
    """P18 (image) values: Commons file names depicting the entity."""
    entity = entity_data(qid, props="claims")
    return _claim_string_values(entity.get("claims") or {}, "P18")


def commons_category(qid: str) -> str:
    """P373 (Commons category) value, without the 'Category:' prefix."""
    entity = entity_data(qid, props="claims")
    values = _claim_string_values(entity.get("claims") or {}, "P373")
    for v in values:
        cat = v.split("|")[-1].strip()
        if cat.lower().startswith("category:"):
            cat = cat[len("category:"):].strip()
        if cat:
            return cat
    return ""


def occupation_qids(qid: str) -> list[str]:
    """P106 (occupation) QIDs — used to tag ancient vs modern sourcing."""
    entity = entity_data(qid, props="claims")
    return _claim_qid_values(entity.get("claims") or {}, "P106")


def resolve_and_cache_person(name: str, *, lang: str = "zh") -> dict[str, Any]:
    """Resolve a person's QID, persist it to persons.qid, and merge Wikidata
    labels/aliases into the person alias set.

    Returns {qid, aliases}. Never raises: failures degrade to whatever is
    already cached locally. Writing is best-effort — the sourcing works off the
    returned values even when the DB is unavailable (e.g. library-only persons
    not yet materialised).
    """
    name = (name or "").strip()
    if not name:
        return {"qid": "", "aliases": []}

    qid = ""
    try:
        from app.services import person_repo

        person = person_repo.resolve_by_name(name)
        if person and str(person.get("qid") or "").startswith("Q"):
            qid = str(person["qid"])
    except Exception:  # noqa: BLE001 — DB 不可用时仍尝试联网解析
        qid = ""

    if not qid:
        qid = resolve_qid(name, lang=lang)
        if not qid and not name.isascii():
            # A Chinese name may be indexed under its English label.
            from app.services.archives import resolve_english_title

            en = resolve_english_title(name)
            if en:
                qid = resolve_qid(en, lang="en")

    aliases = labels_and_aliases(qid) if qid else []

    if qid:
        try:
            from app.services import person_repo

            person_repo.set_person_qid(name, qid)
            for alias in aliases:
                person_repo.add_alias_by_name(name, alias, source="wikidata")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"wikidata: failed to persist qid/aliases for {name!r}: "
                f"{type(exc).__name__}"
            )

    return {"qid": qid, "aliases": aliases}
