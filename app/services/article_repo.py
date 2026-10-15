"""用户上传/粘贴/从史料库添加的文章（里程碑 2 · 人物详情页“文章”标签）。

职责：
* 唯一持有 `mpt.sqlite` 里 `articles` 表的读写，连接来自 `db_migrations.connect`
  （强制外键 / WAL，schema 自动前滚并迁移前备份）。沿用 task_repo / person_repo 的
  单向分层、`BEGIN IMMEDIATE` 短事务与 UTC ISO8601（尾部 Z）时间约定。
* 正文落盘 `storage/articles/<public_id>.txt`（受控文件名，读取只在目录内解析，
  杜绝路径穿越）；表内存 excerpt/word_count 供列表展示，史料库来源走软引用
  （content_ref=`library:<doc_id>`），正文运行时按 doc_id 取。
* 「用于生成」开关（enabled）与权重（weight）持久化；低可信度来源默认不启用
  （reliability ∈ {low, unknown} 且未显式指定 enabled 时置 0），供 UI 标注。

只做数据底座与统计，不参与现有生成链路的改动。
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any

from loguru import logger

from app.services import db_migrations
from app.services.person_repo import connect, now_utc

# 可信度档位：低于此集合视为“低可信”，默认不参与生成，需用户显式开启。
# 用户粘贴/上传的内容由本人负责，默认启用；史料库/外链传 low 时默认不启用并标注。
_LOW_RELIABILITY = {"low"}
_EXCERPT_CHARS = 160


def _txn(conn: sqlite3.Connection):
    class _T:
        def __enter__(self_inner):
            conn.execute("BEGIN IMMEDIATE")
            return conn

        def __exit__(self_inner, exc_type, exc, tb):
            if exc_type is None:
                conn.execute("COMMIT")
            else:
                conn.execute("ROLLBACK")
            return False

    return _T()


def articles_dir() -> str:
    """文章正文目录，env 覆盖保证测试隔离。"""
    override = os.environ.get("MPT_ARTICLES_DIR", "").strip()
    if override:
        os.makedirs(override, exist_ok=True)
        return override
    from app.utils import utils

    return utils.storage_dir("articles", create=True)


def _excerpt(content: str) -> str:
    text = (content or "").strip().replace("\n", " ")
    return text[:_EXCERPT_CHARS]


def _word_count(content: str) -> int:
    # 中文按字符计、英文按词粗计：取非空白字符数作为体量指标（用于展示，非精确）。
    return len([c for c in (content or "") if not c.isspace()])


def _write_body(public_id: str, content: str) -> str:
    """把正文写入 articles 目录，返回受控文件名（basename）。"""
    name = f"{public_id}.txt"
    path = os.path.join(articles_dir(), name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content or "")
    return name


def read_content(article: dict) -> str:
    """按来源读取正文：文件源读盘，史料库软引用尽力从文库取（取不到返回 excerpt）。"""
    ref = (article.get("content_ref") or "").strip()
    if not ref:
        return article.get("excerpt") or ""
    if ref.startswith("library:"):
        doc_id = ref.split(":", 1)[1]
        try:
            from app.services import library

            conn = library.get_conn()
            if conn is not None:
                row = conn.execute(
                    "SELECT content FROM documents WHERE doc_id=?", (doc_id,)
                ).fetchone()
                if row and row[0]:
                    return str(row[0])
        except Exception as exc:  # pragma: no cover - 文库不可用降级到 excerpt
            logger.warning(f"library article body load failed: {type(exc).__name__}: {exc}")
        return article.get("excerpt") or ""
    # 文件源：只在 articles 目录内解析受控 basename，拒绝任何路径分隔符。
    base = os.path.basename(ref)
    path = os.path.join(articles_dir(), base)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return article.get("excerpt") or ""


def add_article(
    person_name: str,
    title: str,
    *,
    source: str = "paste",
    content: str | None = None,
    content_ref: str = "",
    excerpt: str = "",
    reliability: str = "",
    weight: float = 1.0,
    enabled: bool | None = None,
    person_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """新增一篇文章。

    - `content` 为正文（paste/upload）：落盘并生成 content_ref/excerpt/word_count。
    - `content_ref=`library:<doc_id>``（史料库添加）：excerpt 由调用方给出，正文运行时取。
    - `enabled` 留空时：低可信度来源默认不启用（0），否则默认启用（1）。
    """
    own = conn is None
    conn = conn or connect()
    name = (person_name or "").strip()
    public_id = db_migrations.new_public_id("art")
    ts = now_utc()
    body = content if content is not None else ""
    if content is not None:
        ref = _write_body(public_id, body)
        exc = excerpt or _excerpt(body)
        wc = _word_count(body)
    else:
        ref = content_ref
        exc = excerpt or ""
        wc = _word_count(exc)
    if enabled is None:
        enabled = reliability not in _LOW_RELIABILITY
    try:
        with _txn(conn):
            cur = conn.execute(
                """INSERT INTO articles(public_id,person_id,person_name,title,source,
                       content_ref,excerpt,enabled,weight,reliability,word_count,
                       created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    public_id,
                    person_id,
                    name,
                    (title or "").strip()[:120],
                    source or "paste",
                    ref,
                    exc,
                    1 if enabled else 0,
                    float(weight if weight is not None else 1.0),
                    reliability or "",
                    wc,
                    ts,
                    ts,
                ),
            )
            article_id = cur.lastrowid
        row = conn.execute(
            "SELECT * FROM articles WHERE article_id=?", (article_id,)
        ).fetchone()
        return dict(row)
    finally:
        if own:
            conn.close()


def list_articles(person_name: str, conn: sqlite3.Connection | None = None) -> list[dict]:
    own = conn is None
    conn = conn or connect()
    try:
        rows = conn.execute(
            """SELECT article_id,public_id,person_name,title,source,content_ref,
                      excerpt,enabled,weight,reliability,word_count,created_at,
                      updated_at,last_used_at
               FROM articles WHERE person_name=? ORDER BY updated_at DESC""",
            ((person_name or "").strip(),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


def get_article(article_id: int, conn: sqlite3.Connection | None = None) -> dict | None:
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute(
            "SELECT * FROM articles WHERE article_id=?", (int(article_id),)
        ).fetchone()
        return dict(row) if row else None
    finally:
        if own:
            conn.close()


def _update_field(
    article_id: int, column: str, value: Any, conn: sqlite3.Connection | None
) -> dict | None:
    own = conn is None
    conn = conn or connect()
    ts = now_utc()
    try:
        with _txn(conn):
            conn.execute(
                f"UPDATE articles SET {column}=?, updated_at=? WHERE article_id=?",
                (value, ts, int(article_id)),
            )
        return get_article(article_id, conn)
    finally:
        if own:
            conn.close()


def set_enabled(article_id: int, enabled: bool, conn: sqlite3.Connection | None = None) -> dict | None:
    return _update_field(article_id, "enabled", 1 if enabled else 0, conn)


def set_weight(article_id: int, weight: float, conn: sqlite3.Connection | None = None) -> dict | None:
    return _update_field(article_id, "weight", float(weight), conn)


def delete_article(article_id: int, conn: sqlite3.Connection | None = None) -> bool:
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute(
            "SELECT source,content_ref FROM articles WHERE article_id=?", (int(article_id),)
        ).fetchone()
        with _txn(conn):
            cur = conn.execute("DELETE FROM articles WHERE article_id=?", (int(article_id),))
        # 删除文件源的落盘正文（史料库软引用无本地文件）。best-effort，不阻断。
        if row and cur.rowcount > 0 and not (row["content_ref"] or "").startswith("library:"):
            base = os.path.basename(row["content_ref"] or "")
            if base:
                try:
                    os.remove(os.path.join(articles_dir(), base))
                except OSError:
                    pass
        return cur.rowcount > 0
    finally:
        if own:
            conn.close()


def touch_used(article_id: int, conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or connect()
    ts = now_utc()
    try:
        with _txn(conn):
            conn.execute(
                "UPDATE articles SET last_used_at=? WHERE article_id=?", (ts, int(article_id))
            )
    finally:
        if own:
            conn.close()


def enabled_for_generation(person_name: str, conn: sqlite3.Connection | None = None) -> list[dict]:
    """参与生成的文章：enabled=1，按权重降序、创建时间升序。"""
    own = conn is None
    conn = conn or connect()
    try:
        rows = conn.execute(
            """SELECT * FROM articles WHERE person_name=? AND enabled=1
               ORDER BY weight DESC, created_at ASC""",
            ((person_name or "").strip(),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()
