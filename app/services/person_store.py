"""Personal person-archive management layered on top of the read-only library.

The offline pipeline (scripts/library/*) rebuilds ``library.sqlite`` at any time,
so user management data (custom groups, notes, pinned/archived flags, hand-added
persons, edited facts) must never live in that file. This service keeps them in
a separate small database at ``storage/library/personal.sqlite`` and merges
them back into library results via :func:`apply_overlay`.

Data model:
  groups(name PK, created_at)                    — user-defined 自定义分组
  group_members(group_name, person, added_at)    — person ↔ group (M:N)
  person_meta(person PK, dynasty, category, note, hidden, pinned,
              custom, facts_json, updated_at)    — overlay per person

Semantics:
  * dynasty/category non-empty override the library-derived value;
  * facts_json non-NULL replaces the aggregated key facts;
  * hidden=1 removes a person from listings and blanks its profile
    (soft delete for library persons, real delete for custom ones);
  * custom=1 marks persons created by the user that do not exist in the
    library corpus.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from app.utils import utils

DB_NAME = "personal.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS groups(
  name TEXT PRIMARY KEY,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS group_members(
  group_name TEXT NOT NULL,
  person TEXT NOT NULL,
  added_at TEXT NOT NULL,
  PRIMARY KEY(group_name, person)
);
CREATE TABLE IF NOT EXISTS person_meta(
  person TEXT PRIMARY KEY,
  dynasty TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  hidden INTEGER NOT NULL DEFAULT 0,
  pinned INTEGER NOT NULL DEFAULT 0,
  custom INTEGER NOT NULL DEFAULT 0,
  facts_json TEXT,
  updated_at TEXT NOT NULL
);
"""


def db_path() -> Path:
    """Personal db location; env override keeps tests hermetic."""
    override = os.environ.get("MPT_PERSONAL_DB", "").strip()
    if override:
        return Path(override)
    return Path(utils.root_dir()) / "storage" / "library" / DB_NAME


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clean_name(name: str) -> str:
    return (name or "").strip()[:40]


# ------------------------------------------------------------------ groups


def list_groups() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT g.name, g.created_at, "
            "  (SELECT COUNT(*) FROM group_members m WHERE m.group_name=g.name) AS n "
            "FROM groups g ORDER BY g.created_at"
        ).fetchall()
    return [{"name": r["name"], "created_at": r["created_at"], "count": r["n"]} for r in rows]


def create_group(name: str) -> bool:
    name = _clean_name(name)
    if not name:
        return False
    with _connect() as conn:
        try:
            conn.execute(
                "INSERT INTO groups(name, created_at) VALUES (?, ?)", (name, _now())
            )
        except sqlite3.IntegrityError:
            return False
    return True


def delete_group(name: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM groups WHERE name = ?", (name,))
        conn.execute("DELETE FROM group_members WHERE group_name = ?", (name,))
        return cur.rowcount > 0


def groups_of(person: str) -> list[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT group_name FROM group_members WHERE person = ? "
            "ORDER BY added_at",
            (_clean_name(person),),
        ).fetchall()
    return [r["group_name"] for r in rows]


def set_person_groups(person: str, group_names: list[str]) -> None:
    """Replace the full group membership of one person; unknown groups ignored."""
    person = _clean_name(person)
    if not person:
        return
    with _connect() as conn:
        known = {
            r["name"] for r in conn.execute("SELECT name FROM groups").fetchall()
        }
        wanted = [g for g in dict.fromkeys(group_names or []) if g in known]
        conn.execute("DELETE FROM group_members WHERE person = ?", (person,))
        now = _now()
        conn.executemany(
            "INSERT OR IGNORE INTO group_members(group_name, person, added_at) "
            "VALUES (?,?,?)",
            [(g, person, now) for g in wanted],
        )
        if wanted:
            # 有分组关系时保留 meta 行，列表合并时能带出分组信息
            _ensure_meta(conn, person)


# ------------------------------------------------------------------ person meta


def person_meta(person: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM person_meta WHERE person = ?", (_clean_name(person),)
        ).fetchone()
    return dict(row) if row else None


def _ensure_meta(conn: sqlite3.Connection, person: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM person_meta WHERE person = ?", (person,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO person_meta(person, updated_at) VALUES (?, ?)",
            (person, _now()),
        )
        row = conn.execute(
            "SELECT * FROM person_meta WHERE person = ?", (person,)
        ).fetchone()
    return row


def update_person_meta(
    person: str,
    *,
    dynasty: str | None = None,
    category: str | None = None,
    note: str | None = None,
    facts: list[str] | None = None,
    pinned: bool | None = None,
) -> bool:
    """Patch overlay fields for one person (library or custom)."""
    person = _clean_name(person)
    if not person:
        return False
    with _connect() as conn:
        row = _ensure_meta(conn, person)
        sets: list[str] = []
        args: list[Any] = []
        if dynasty is not None:
            sets.append("dynasty = ?")
            args.append(dynasty.strip()[:20])
        if category is not None:
            sets.append("category = ?")
            args.append(category.strip()[:20])
        if note is not None:
            sets.append("note = ?")
            args.append(note.strip()[:2000])
        if facts is not None:
            sets.append("facts_json = ?")
            args.append(json.dumps([f for f in facts if f.strip()], ensure_ascii=False))
        if pinned is not None:
            sets.append("pinned = ?")
            args.append(1 if pinned else 0)
        if sets:
            sets.append("updated_at = ?")
            args.append(_now())
            args.append(person)
            conn.execute(
                f"UPDATE person_meta SET {', '.join(sets)} WHERE person = ?", args
            )
    return True


def _in_library(person: str) -> bool:
    """Whether the name exists in the rebuildable library corpus."""
    try:
        from app.services import library

        conn = library.get_conn()
        if conn is None:
            return False
        return (
            conn.execute(
                "SELECT 1 FROM persons WHERE person = ? LIMIT 1", (person,)
            ).fetchone()
            is not None
        )
    except sqlite3.Error:
        return False


def create_custom_person(
    person: str,
    *,
    dynasty: str = "",
    category: str = "",
    facts: list[str] | None = None,
    note: str = "",
) -> bool:
    person = _clean_name(person)
    if not person:
        return False
    with _connect() as conn:
        row = _ensure_meta(conn, person)
        # 与文库重名的“新建”实际是给文库人物叠加编辑，不能标记为自建，
        # 否则删除时会把叠加层当作自建档案硬删而不是归档。
        custom = 0 if _in_library(person) else 1
        conn.execute(
            "UPDATE person_meta SET custom = ?, dynasty = ?, category = ?, note = ?, "
            "facts_json = ?, hidden = 0, updated_at = ? WHERE person = ?",
            (
                custom,
                dynasty.strip()[:20],
                category.strip()[:20],
                note.strip()[:2000],
                json.dumps([f for f in (facts or []) if f.strip()], ensure_ascii=False),
                _now(),
                person,
            ),
        )
        if row["hidden"]:
            # reviving an archived name as custom is intentional
            logger.info(f"custom person revived from archive: {person}")
    return True


def delete_person(person: str) -> str:
    """Custom persons are removed outright; library figures are archived (hidden).

    与文库同名的人物即使带 custom 标记也按归档处理，避免丢失叠加层背后的
    文库档案。Returns ``"deleted"``, ``"archived"`` or ``"not_found"``.
    """
    person = _clean_name(person)
    if not person:
        return "not_found"
    in_library = _in_library(person)
    with _connect() as conn:
        row = conn.execute(
            "SELECT custom FROM person_meta WHERE person = ?", (person,)
        ).fetchone()
        if row is not None and row["custom"] and not in_library:
            conn.execute("DELETE FROM person_meta WHERE person = ?", (person,))
            conn.execute("DELETE FROM group_members WHERE person = ?", (person,))
            return "deleted"
        _ensure_meta(conn, person)
        conn.execute(
            "UPDATE person_meta SET hidden = 1, updated_at = ? WHERE person = ?",
            (_now(), person),
        )
        return "archived"


def restore_person(person: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE person_meta SET hidden = 0, updated_at = ? "
            "WHERE person = ? AND hidden = 1",
            (_now(), _clean_name(person)),
        )
        return cur.rowcount > 0


def hidden_persons() -> set[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT person FROM person_meta WHERE hidden = 1"
        ).fetchall()
    return {r["person"] for r in rows}


# ------------------------------------------------------------------ listing


def _library_persons() -> list[dict[str, Any]]:
    from app.services import library

    conn = library.get_conn()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT person, dynasty, doc_count, reliable_high FROM persons"
        ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def person_category_map() -> dict[str, str]:
    """Primary content category per person: the modal category of the articles
    where the person is the first-listed (main) subject."""
    from app.services import library

    conn = library.get_conn()
    if conn is None:
        return {}
    try:
        rows = conn.execute(
            "SELECT json_extract(a.json, '$.persons[0]') AS person, "
            "       d.category AS cat, COUNT(*) AS n "
            "FROM annotations a JOIN documents d ON d.doc_id = a.target_id "
            "WHERE a.target_type = 'doc' AND person IS NOT NULL AND person != '' "
            "GROUP BY person, cat"
        ).fetchall()
    except sqlite3.Error:
        return {}
    best: dict[str, tuple[str, int]] = {}
    for r in rows:
        person = (r["person"] or "").strip()
        if not person:
            continue
        if person not in best or r["n"] > best[person][1]:
            best[person] = (r["cat"] or "", r["n"])
    return {p: c for p, (c, _) in best.items()}


def list_persons(
    *,
    dynasty: str = "",
    category: str = "",
    group: str = "",
    keyword: str = "",
    include_hidden: bool = False,
) -> list[dict[str, Any]]:
    """Merged person listing for browsing/management.

    Combines library figures with user-created persons, applies overlay
    dynasty/category, filters by the three category dimensions plus keyword,
    and sorts pinned first, then by corpus article count.
    """
    library_rows = _library_persons()
    cat_map = person_category_map()
    with _connect() as conn:
        metas = {
            r["person"]: dict(r)
            for r in conn.execute("SELECT * FROM person_meta").fetchall()
        }
        member_map: dict[str, list[str]] = {}
        for r in conn.execute(
            "SELECT group_name, person FROM group_members"
        ).fetchall():
            member_map.setdefault(r["person"], []).append(r["group_name"])

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in library_rows:
        person = row["person"]
        seen.add(person)
        meta = metas.get(person) or {}
        if meta.get("hidden") and not include_hidden:
            continue
        eff_dynasty = (meta.get("dynasty") or "").strip() or (row["dynasty"] or "")
        eff_category = (meta.get("category") or "").strip() or cat_map.get(person, "")
        groups = member_map.get(person, [])
        if dynasty and eff_dynasty != dynasty:
            continue
        if category and eff_category != category:
            continue
        if group and group not in groups:
            continue
        if keyword and keyword.lower() not in person.lower():
            continue
        result.append({
            "person": person,
            "dynasty": eff_dynasty,
            "category": eff_category,
            "doc_count": row["doc_count"],
            "source": "library",
            "pinned": bool(meta.get("pinned")),
            "hidden": bool(meta.get("hidden")),
            "note": meta.get("note") or "",
            "groups": groups,
        })

    for person, meta in metas.items():
        if person in seen or not meta.get("custom"):
            continue
        if meta.get("hidden") and not include_hidden:
            continue
        groups = member_map.get(person, [])
        if dynasty and (meta.get("dynasty") or "") != dynasty:
            continue
        if category and (meta.get("category") or "") != category:
            continue
        if group and group not in groups:
            continue
        if keyword and keyword.lower() not in person.lower():
            continue
        result.append({
            "person": person,
            "dynasty": meta.get("dynasty") or "",
            "category": meta.get("category") or "",
            "doc_count": 0,
            "source": "custom",
            "pinned": bool(meta.get("pinned")),
            "hidden": bool(meta.get("hidden")),
            "note": meta.get("note") or "",
            "groups": groups,
        })

    result.sort(key=lambda x: (not x["pinned"], -x["doc_count"], x["person"]))
    return result


def dynasty_options() -> list[str]:
    """Dynasties present in the corpus plus overlay values, by person count."""
    counts: dict[str, int] = {}
    with _connect() as conn:
        metas = {r["person"]: dict(r) for r in conn.execute("SELECT * FROM person_meta")}
    for row in _library_persons():
        meta = metas.pop(row["person"], None) or {}
        if meta.get("hidden"):
            continue
        dyn = (meta.get("dynasty") or "").strip() or (row["dynasty"] or "")
        if dyn:
            counts[dyn] = counts.get(dyn, 0) + 1
    # 自定义人物的朝代不来自文库，单独补进选项（不计入排序权重）。
    for meta in metas.values():
        if not meta.get("custom") or meta.get("hidden"):
            continue
        dyn = (meta.get("dynasty") or "").strip()
        if dyn:
            counts.setdefault(dyn, 0)
    return sorted(counts, key=lambda d: (-counts[d], d))


def category_options() -> list[str]:
    """Content categories of the corpus (documents.category) plus custom values."""
    from app.services import library

    conn = library.get_conn()
    options: list[str] = []
    if conn is not None:
        try:
            options = [
                r[0]
                for r in conn.execute(
                    "SELECT category, COUNT(*) c FROM documents "
                    "WHERE category != '' GROUP BY category ORDER BY c DESC"
                )
            ]
        except sqlite3.Error:
            options = []
    with _connect() as cur:
        for r in cur.execute(
            "SELECT DISTINCT category FROM person_meta "
            "WHERE custom = 1 AND category != ''"
        ):
            if r[0] not in options:
                options.append(r[0])
    return options


# ------------------------------------------------------------------ profile overlay


def custom_profile(person: str) -> dict[str, Any] | None:
    """Profile for a user-created person (no library corpus behind it)."""
    meta = person_meta(person)
    if not meta or not meta.get("custom"):
        return None
    if meta.get("hidden"):
        return None
    try:
        facts = json.loads(meta.get("facts_json") or "[]")
    except json.JSONDecodeError:
        facts = []
    return {
        "person": person,
        "dynasty": meta.get("dynasty") or None,
        "key_facts": facts,
        "docs": [],
        "books": [],
        "era_en": "",
        "note": meta.get("note") or "",
        "groups": groups_of(person),
        "pinned": bool(meta.get("pinned")),
        "source": "custom",
    }


def apply_overlay(profile: dict[str, Any]) -> dict[str, Any] | None:
    """Merge personal overlay into a library profile.

    Returns None when the person is archived (hidden), so downstream grounding
    and the WebUI both stop using the figure consistently.
    """
    person = profile.get("person") or ""
    meta = person_meta(person)
    if meta is not None and meta.get("hidden"):
        return None
    if meta is not None:
        if (meta.get("dynasty") or "").strip():
            profile["dynasty"] = meta["dynasty"].strip()
        if meta.get("facts_json") is not None:
            try:
                profile["key_facts"] = json.loads(meta["facts_json"])
            except json.JSONDecodeError:
                pass
    if meta is not None and (meta.get("category") or "").strip():
        profile["category"] = meta["category"].strip()
    else:
        # 未经编辑的人物也带上从文库推导的内容类别，供展示使用。
        profile["category"] = person_category_map().get(person, "")
    if meta is None:
        return profile
    profile["note"] = meta.get("note") or ""
    profile["pinned"] = bool(meta.get("pinned"))
    profile["groups"] = groups_of(person)
    return profile


def get_person(name: str) -> dict[str, Any] | None:
    """Overlay-aware profile: library first, custom persons as fallback."""
    from app.services import library

    profile = library.get_person_profile(name)
    if profile is not None:
        return profile  # get_person_profile already applies apply_overlay
    return custom_profile(name)
