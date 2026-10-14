"""Biography source library: read-only access to storage/library/library.sqlite.

Offline pipeline (scripts/library/*) ingests:
  source A: 中国名人古今人物传记 (~4942 historical articles, GB18030 .doc)
  source B: 人物传记合集 (~1256 books, mobi/epub/azw3 -> chapters)
and LLM-annotates them (persons, dynasty, years, doc_type, reliability).

This module gives the generation pipeline a *factual grounding* layer:

  profile = library.get_person_profile("曹操")
  profile["dynasty"], profile["era_en"], profile["key_facts"], profile["docs"]

Hard rule: book/article bodies come from pirated sources — this API must
NEVER return long verbatim quotes into prompts; only facts, titles and short
snippets (< MAX_SNIPPET chars) for context. Downstream prompts rewrite
everything into original narration.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any

from loguru import logger

from app.config import config
from app.utils import utils

DB_NAME = "library.sqlite"
MAX_SNIPPET = 160


def enabled() -> bool:
    return bool(config.app.get("library_enabled", True))


def db_path() -> Path | None:
    override = os.environ.get("MPT_LIBRARY_DB", "").strip()
    if override:
        p = Path(override)
        return p if p.is_file() else None
    p = Path(utils.root_dir()) / "storage" / "library" / DB_NAME
    return p if p.is_file() else None


@lru_cache(maxsize=2)
def _conn(path_str: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path_str, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def get_conn() -> sqlite3.Connection | None:
    if not enabled():
        return None
    p = db_path()
    if p is None:
        return None
    try:
        conn = _conn(str(p))
        # fail fast when the pipeline has not produced tables yet
        conn.execute("SELECT 1 FROM documents LIMIT 1").fetchone()
        return conn
    except sqlite3.Error as exc:
        logger.warning(f"library db unavailable: {exc}")
        return None


def is_biography_source(source: str | None) -> bool:
    """仅 ``historical_archive`` 走传记/史料素材链路,判定器收敛在这里。"""
    return bool(source and str(source).strip() == "historical_archive")


def all_doc_ids(profile: dict[str, Any] | None) -> list[str]:
    """从人物档案提取所有传记文章 doc_id:去重、保序、去空白、跳过 None/缺失。"""
    if not isinstance(profile, dict):
        return []
    docs = profile.get("docs") or []
    if not isinstance(docs, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for d in docs:
        if not isinstance(d, dict):
            continue
        raw = d.get("doc_id")
        if raw is None:
            continue
        doc_id = str(raw)
        if not doc_id.strip():
            continue
        key = doc_id.strip()
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def default_base_script_ids(profile: dict[str, Any] | None) -> list[str]:
    """默认基础脚本选择 = 该人物全部传记文章(自动全选)。"""
    return all_doc_ids(profile)


def get_docs(doc_ids: list[str] | None) -> list[dict[str, Any]]:
    """按 doc_id 批量读取史料全文(传记/基础脚本原文),保序、跳过缺失。

    只返回有正文的文章;缺失/不存在的 doc_id 被静默跳过。每篇带
    ``doc_id/category/title/body/n_chars`` 供材料渲染与逐篇截断使用。
    """
    if not doc_ids:
        return []
    conn = get_conn()
    if conn is None:
        return []
    # 去空白并保序去重,避免重复查询与重复素材
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in doc_ids:
        sid = str(raw).strip()
        if not sid or sid in seen:
            continue
        seen.add(sid)
        cleaned.append(sid)
    if not cleaned:
        return []
    marks = ",".join("?" for _ in cleaned)
    rows: list[dict[str, Any]] = []
    try:
        for r in conn.execute(
            f"SELECT doc_id, category, title, body, n_chars FROM documents "
            f"WHERE doc_id IN ({marks}) ORDER BY rowid",
            cleaned,
        ).fetchall():
            body = str(r[3] or "").strip()
            rows.append({
                "doc_id": r[0],
                "category": r[1] or "",
                "title": r[2] or "",
                "body": body,
                "n_chars": int(r[4] or 0),
            })
    except sqlite3.Error as exc:
        logger.warning(f"get_docs failed: {exc}")
        return []
    # 按请求顺序返回(存在的才保留);用 map 保持原请求顺序而非 rowid 顺序
    by_id = {r["doc_id"]: r for r in rows}
    out: list[dict[str, Any]] = []
    for sid in cleaned:
        r = by_id.get(sid)
        if r is None:
            continue
        out.append(r)
    return out



def _seg(text: str) -> str:
    """Per-character CJK tokenization matching the offline FTS index."""
    out = []
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            out.extend([" ", ch, " "])
        elif ch.isalnum() and ch.isascii():
            out.append(ch.lower())
        else:
            out.append(" ")
    return " ".join("".join(out).split())


def _query_terms(query: str) -> list[str]:
    """Raw search terms for snippet windows (whole query first, then bigrams)."""
    terms = [query.strip()] if query and len(query.strip()) <= 12 else []
    cjk = re.findall(r"[\u4e00-\u9fff]{2,}", query)
    for run in cjk:
        for i in range(len(run) - 1):
            bigram = run[i:i + 2]
            if bigram not in terms:
                terms.append(bigram)
    latin = re.findall(r"[A-Za-z0-9]{3,}", query)
    terms.extend(tok for tok in latin if tok not in terms)
    return terms[:6] or [query.strip()]


def _window(body: str, terms: list[str], maxlen: int = MAX_SNIPPET) -> str:
    """Short window around the first matched term — our own snippet().

    The FTS tables are contentless (content='') for index size, so SQLite's
    snippet() returns nothing; windows are computed from the content tables.
    """
    if not body:
        return ""
    pos = -1
    for t in terms:
        p = body.find(t)
        if p >= 0 and (pos < 0 or p < pos):
            pos = p
    if pos < 0:
        pos = 0
    start = max(0, pos - maxlen // 3)
    end = min(len(body), start + maxlen)
    snippet = body[start:end].replace("\n", " ")
    snippet = re.sub(r"\s+", " ", snippet).strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(body):
        snippet += "…"
    return snippet


def _match_query(query: str) -> str:
    """Build an FTS5 MATCH expression from free text.

    The index stores CJK as space-separated single chars, so adjacency
    phrases must be written with spaces inside the quotes ("官 渡"),
    otherwise unicode61 tokenizes the phrase as one unmatchable token.
    Phrases are built per whitespace-delimited word so multi-word queries
    (曼德拉 罗本岛) AND the terms instead of forcing cross-word adjacency.
    """
    parts: list[str] = []
    for word in re.split(r"\s+", query.strip()):
        if not word:
            continue
        chars = re.split(r"\s+", _seg(word))
        cjk = [t for t in chars if "\u4e00" <= t <= "\u9fff"]
        latin = [t for t in chars if t.isascii() and t.isalnum()]
        if len(cjk) >= 2:
            parts.append(f'"{" ".join(cjk)}"')
        elif cjk:
            parts.append(cjk[0])
        parts.extend(latin)
    return " AND ".join(parts)


def get_person_profile(name: str, max_docs: int = 12) -> dict[str, Any] | None:
    """Aggregated biography facts for one person, or None when unknown."""
    conn = get_conn()
    if conn is None or not name or not name.strip():
        return None
    name = name.strip()
    profile: dict[str, Any] = {"person": name}

    row = None
    try:
        row = conn.execute(
            "SELECT * FROM persons WHERE person = ?", (name,)
        ).fetchone()
    except sqlite3.Error:
        pass

    top_docs: list[str] = []
    if row:
        profile["dynasty"] = row["dynasty"]
        profile["doc_count"] = row["doc_count"]
        profile["reliable_high"] = row["reliable_high"]
        try:
            profile["key_facts"] = json.loads(row["facts_json"] or "[]")
        except json.JSONDecodeError:
            profile["key_facts"] = []
        try:
            top_docs = json.loads(row["top_docs"] or "[]")[:max_docs]
        except json.JSONDecodeError:
            top_docs = []
    else:
        profile["dynasty"] = None
        profile["key_facts"] = []

    # Fallback: articles whose TITLE names this person. A plain FTS fallback
    # would match per-character noise (钱/学/森 appear in unrelated texts), so
    # require the full name in the title instead.
    if not top_docs:
        try:
            docs = conn.execute(
                "SELECT doc_id FROM documents WHERE title LIKE ? LIMIT ?",
                (f"%{name}%", max_docs),
            ).fetchall()
            top_docs = [d["doc_id"] for d in docs]
        except sqlite3.Error:
            top_docs = []

    docs_meta = []
    if top_docs:
        marks = ",".join("?" * len(top_docs))
        try:
            docs_meta = conn.execute(
                f"SELECT doc_id, category, title, n_chars FROM documents "
                f"WHERE doc_id IN ({marks})",
                top_docs,
            ).fetchall()
        except sqlite3.Error:
            docs_meta = []
    profile["docs"] = [dict(d) for d in docs_meta]

    books = []
    try:
        annos = conn.execute(
            "SELECT b.title, b.author, a.json FROM books b "
            "JOIN annotations a ON a.target_id = b.book_id "
            "WHERE a.target_type='book' AND json_extract(a.json,'$.subject') = ?",
            (name,),
        ).fetchall()
        for x in annos:
            try:
                meta = json.loads(x["json"])
            except json.JSONDecodeError:
                continue
            books.append({
                "title": x["title"], "author": x["author"],
                "era_hint": meta.get("era_hint", ""),
                "dynasty": meta.get("dynasty", ""),
                "genre": meta.get("genre", ""),
            })
    except sqlite3.Error:
        pass
    profile["books"] = books
    # A modern/foreign figure may have no article-derived dynasty; the book
    # annotation (`走近钱学森` -> 当代) is the next best source.
    if not profile.get("dynasty"):
        for b in books:
            if b.get("dynasty"):
                profile["dynasty"] = b["dynasty"]
                break
    profile["era_en"] = books[0]["era_hint"] if books else ""

    if not (profile["docs"] or books or profile["key_facts"] or row):
        return None
    # 叠加个人库(自定义朝代/类别/备注/事实、置顶、归档状态)。
    # 归档(hidden=1)时 apply_overlay 返回 None,即归档人物在 grounding/展示
    # 链路上同步"消失";编辑后的朝代/事实/类别则覆盖文库原值。此处理放在
    # 内部(库层)而非外部,保证 dynasty_of/era_keywords/grounding_block/mpt_admin
    # 及 person_store.get_person 全部走同一份"已叠加"的档案,不再各维护一份。
    try:
        from app.services import person_store

        return person_store.apply_overlay(profile)
    except Exception as exc:  # pragma: no cover — overlay 失败不应回退到未叠加原始档案
        logger.warning(f"apply_overlay failed for {name!r}: {type(exc).__name__}: {exc}")
        return profile


def search_facts(
    query: str, limit: int = 8, category: str | None = None
) -> list[dict[str, Any]]:
    """FTS5 search over articles -> {title, category, snippet, reliable}.

    Snippets are short (<MAX_SNIPPET) by design; they exist to show the LLM
    which facts are covered locally, not to quote the book.
    """
    conn = get_conn()
    if conn is None:
        return []
    sql = (
        "SELECT d.doc_id, d.category, d.title, d.body "
        "FROM docs_fts f JOIN documents d ON d.rowid = f.rowid "
        "WHERE docs_fts MATCH ?"
    )
    args: list[Any] = [_match_query(query)]
    if category:
        sql += " AND d.category = ?"
        args.append(category)
    sql += " ORDER BY rank LIMIT ?"
    args.append(limit)
    try:
        rows = conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []
    terms = _query_terms(query)
    out = []
    for r in rows:
        reliable = None
        ann = conn.execute(
            "SELECT json FROM annotations WHERE target_type='doc' AND target_id=?",
            (r["doc_id"],),
        ).fetchone()
        if ann:
            try:
                reliable = json.loads(ann["json"]).get("reliable")
            except json.JSONDecodeError:
                pass
        out.append({
            "title": r["title"],
            "category": r["category"],
            "snippet": _window(r["body"], terms),
            "reliable": reliable,
        })
    return out


def search_book_excerpts(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """FTS5 over books -> {title, author, dynasty, snippet(short)}.

    books_fts.rowid is aligned with books.rowid by the offline builder.
    The FTS index is contentless, so snippet windows are read from chapters.
    """
    conn = get_conn()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT b.book_id, b.title, b.author, a.json AS anno "
            "FROM books_fts f JOIN books b ON b.rowid = f.rowid "
            "LEFT JOIN annotations a ON a.target_id = b.book_id "
            "  AND a.target_type='book' "
            "WHERE books_fts MATCH ? ORDER BY rank LIMIT ?",
            (_match_query(query), limit),
        ).fetchall()
    except sqlite3.Error:
        return []
    terms = _query_terms(query)
    out = []
    for r in rows:
        dynasty = ""
        if r["anno"]:
            try:
                dynasty = json.loads(r["anno"]).get("dynasty", "")
            except json.JSONDecodeError:
                pass
        snippet = ""
        for term in terms:
            hit = conn.execute(
                "SELECT body FROM chapters WHERE book_id=? "
                "AND instr(body, ?) > 0 LIMIT 1",
                (r["book_id"], term),
            ).fetchone()
            if hit:
                snippet = _window(hit["body"], [term])
                if snippet:
                    break
        out.append({
            "title": r["title"], "author": r["author"], "dynasty": dynasty,
            "snippet": snippet,
        })
    return out


def dynasty_of(name: str) -> str | None:
    return (get_person_profile(name, max_docs=1) or {}).get("dynasty") or None


def era_keywords(name: str, fallback: str = "") -> str:
    profile = get_person_profile(name, max_docs=1)
    if not profile:
        return fallback
    return profile.get("era_en") or fallback


def grounding_block(name: str, max_facts: int = 8) -> str:
    """Compact, prompt-ready biography grounding text for one person."""
    profile = get_person_profile(name)
    if not profile:
        return ""
    lines = [f"【本地史料库 — {name}】"]
    if profile.get("dynasty"):
        lines.append(f"时代: {profile['dynasty']}"
                     + (f" (英文检索词: {profile['era_en']})" if profile.get("era_en") else ""))
    for f in (profile.get("key_facts") or [])[:max_facts]:
        lines.append(f"- {f}")
    if profile.get("docs"):
        titles = "、".join(d["title"][:24] for d in profile["docs"][:5])
        lines.append(f"相关史料文章: {len(profile['docs'])} 篇，如: {titles}")
    if profile.get("books"):
        lines.append("参考书目: " + "；".join(b["title"][:30] for b in profile["books"][:3]))
    return "\n".join(lines)
