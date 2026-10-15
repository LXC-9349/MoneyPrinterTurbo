"""人物聚合根数据访问层（阶段 2 步骤 1，纯新增、尚未接线到 controller）。

职责（见设计文档 §2.2/§2.7）：
* 唯一持有 `mpt.sqlite` 读写；连接来自 `db_migrations.connect`（强制外键/WAL）。
* 确立单向分层，替代 library⇄person_store 双向环（步骤 4 才摘旧环）。
* **写用 `BEGIN IMMEDIATE` 短事务**；连接不跨线程共享（每调用开/关或传入连接）。
* 时间统一 UTC ISO8601（尾部 Z）。
* 删除：软删除只改 `state='archived'`；硬删除事务内删行→提交后删文件；
  `reconcile()` 标 `file_missing`、清孤儿（**存储清理只经本模块**）。

本步骤只提供 repo 能力与建表，不改动任何现有调用路径。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from app.services import db_migrations
from app.utils import utils

DB_NAME = "mpt.sqlite"


@contextmanager
def _txn(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE 短事务：写锁尽早获取，成功 COMMIT、异常 ROLLBACK。

    连接为 autocommit（isolation_level=None），事务完全显式，不与隐式事务冲突。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def db_path() -> str:
    """主库路径：env `MPT_MAIN_DB`，默认与个人覆盖层同目录 storage/library/。"""
    env = os.environ.get("MPT_MAIN_DB", "").strip()
    if env:
        return env
    return os.path.join(utils.storage_dir("library", create=True), DB_NAME)


def connect(path: str | None = None) -> sqlite3.Connection:
    """开一个已迁移到最新 schema 的连接（调用方负责 close）。"""
    conn = db_migrations.connect(path or db_path())
    db_migrations.ensure_migrated(conn)
    return conn


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


# ── Person ──────────────────────────────────────────────────────────────
def get_person(public_id: str, conn: sqlite3.Connection | None = None) -> dict | None:
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute(
            "SELECT * FROM persons WHERE public_id=?", (public_id,)
        ).fetchone()
        return _row_to_dict(row)
    finally:
        if own:
            conn.close()


# ── 结构化事实（里程碑 2 详情页“事实”标签：增删改 + 核验/人工确认）──────
def list_facts(display_name: str, conn: sqlite3.Connection | None = None) -> list[dict]:
    """按主名查人物事实行；未物化人物返回空列表（与 list_person_assets 一致）。"""
    own = conn is None
    conn = conn or connect()
    try:
        pid = _person_id_by_name(display_name, conn)
        if pid is None:
            return []
        rows = conn.execute(
            """SELECT fact_id, ordinal, statement, source, source_ref, verified,
                      human_confirmed, edited_by_user
               FROM facts WHERE person_id=? ORDER BY ordinal, fact_id""",
            (pid,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


def update_fact(
    fact_id: int,
    *,
    statement: str | None = None,
    verified: int | None = None,
    human_confirmed: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict | None:
    """编辑事实：改正文必标 edited_by_user=1（人工修订可追溯）；返回最新行，
    不存在返回 None。只更新显式传入的字段。"""
    own = conn is None
    conn = conn or connect()
    try:
        sets: list[str] = []
        args: list[Any] = []
        if statement is not None:
            sets.append("statement=?")
            args.append(statement)
            sets.append("edited_by_user=1")
        if verified is not None:
            sets.append("verified=?")
            args.append(int(verified))
        if human_confirmed is not None:
            sets.append("human_confirmed=?")
            args.append(int(human_confirmed))
        if not sets:
            row = conn.execute("SELECT * FROM facts WHERE fact_id=?", (fact_id,)).fetchone()
            return _row_to_dict(row)
        args.append(fact_id)
        with _txn(conn):
            cur = conn.execute(f"UPDATE facts SET {', '.join(sets)} WHERE fact_id=?", args)
        if cur.rowcount == 0:
            return None
        row = conn.execute("SELECT * FROM facts WHERE fact_id=?", (fact_id,)).fetchone()
        return _row_to_dict(row)
    finally:
        if own:
            conn.close()


def delete_fact(fact_id: int, conn: sqlite3.Connection | None = None) -> bool:
    """删除（忽略）单条事实；返回是否命中行。"""
    own = conn is None
    conn = conn or connect()
    try:
        with _txn(conn):
            cur = conn.execute("DELETE FROM facts WHERE fact_id=?", (fact_id,))
        return cur.rowcount > 0
    finally:
        if own:
            conn.close()


def import_overlay_facts(
    display_name: str, statements: list[str], conn: sqlite3.Connection | None = None
) -> int:
    """把旧覆盖层 key_facts 字符串数组一次性迁入 facts 表（惰性迁移）。

    仅在该人物 facts 表无任何行时写入，幂等；来源标 overlay、未核验
    （verified=0）——由用户在事实标签里采纳/修改/忽略后才有确认态。
    返回导入条数。
    """
    items = [str(s).strip() for s in (statements or []) if str(s).strip()]
    if not items:
        return 0
    own = conn is None
    conn = conn or connect()
    try:
        person = create_or_get_person(display_name, conn=conn)
        pid = person["person_id"]
        has = conn.execute(
            "SELECT 1 FROM facts WHERE person_id=? LIMIT 1", (pid,)
        ).fetchone()
        if has:
            return 0
        with _txn(conn):
            for i, stmt in enumerate(items):
                conn.execute(
                    "INSERT INTO facts(person_id,ordinal,statement,source,verified,"
                    "human_confirmed,edited_by_user) VALUES(?,?,?,?,0,0,0)",
                    (pid, i, stmt, "overlay"),
                )
        return len(items)
    finally:
        if own:
            conn.close()


# ── 人物图片素材（里程碑 2 详情页“图片素材”标签）────────────────────────
def list_person_assets(display_name: str, conn: sqlite3.Connection | None = None) -> list[dict]:
    """按主名查已物化人物的图片素材（含封面标志/来源/许可）。

    史料库人物若尚未物化到 persons 表，返回空列表（图片在采纳/建档后才登记）。
    """
    own = conn is None
    conn = conn or connect()
    try:
        person = conn.execute(
            "SELECT person_id FROM persons WHERE display_name=? AND state='active' LIMIT 1",
            ((display_name or "").strip(),),
        ).fetchone()
        if not person:
            return []
        rows = conn.execute(
            """SELECT a.asset_id, a.kind, a.path, a.provider, a.license, a.license_url,
                      a.author, a.source_page, a.source_title, pa.state, pa.is_cover, pa.role
               FROM assets a JOIN person_assets pa ON pa.asset_id = a.asset_id
               WHERE pa.person_id=? AND a.state='active'
               ORDER BY pa.is_cover DESC, a.created_at DESC""",
            (person["person_id"],),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


def resolve_by_name(name: str, conn: sqlite3.Connection | None = None) -> dict | None:
    """姓名/别名/拼音 → 人物；沿 `merged_into` 重定向到存活方（限深度防环）。"""
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute(
            """
            SELECT p.* FROM persons p
            JOIN person_aliases a ON a.person_id = p.person_id
            WHERE a.alias = ?
            ORDER BY p.state = 'active' DESC, p.person_id
            LIMIT 1
            """,
            (name,),
        ).fetchone()
        person = _row_to_dict(row)
        seen = 0
        while person and person.get("merged_into") and seen < 10:
            person = get_person(person["merged_into"], conn)
            seen += 1
        return person
    finally:
        if own:
            conn.close()


def create_or_get_person(
    display_name: str,
    *,
    dynasty: str = "",
    category: str = "",
    note: str = "",
    origin: str = "library",
    qid: str = "",
    conn: sqlite3.Connection | None = None,
) -> dict:
    """惰性物化入口：按精确同名（active）复用，否则新建 public_id。幂等。"""
    own = conn is None
    conn = conn or connect()
    name = (display_name or "").strip()
    if not name:
        raise ValueError("display_name is required")
    try:
        existing = conn.execute(
            "SELECT * FROM persons WHERE display_name=? AND state='active' LIMIT 1",
            (name,),
        ).fetchone()
        if existing:
            with _txn(conn):
                _ensure_alias(conn, existing["person_id"], name)
            return _row_to_dict(existing)

        pid = db_migrations.new_public_id("per")
        ts = now_utc()
        with _txn(conn):
            cur = conn.execute(
                """INSERT INTO persons(public_id,qid,display_name,dynasty,category,note,
                       origin,state,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,'active',?,?)""",
                (pid, qid, name, dynasty, category, note, origin, ts, ts),
            )
            person_id = cur.lastrowid
            _ensure_alias(conn, person_id, name)
        return get_person(pid, conn)  # type: ignore[return-value]
    finally:
        if own:
            conn.close()


def _ensure_alias(conn: sqlite3.Connection, person_id: int, alias: str, source: str = "") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO person_aliases(person_id,alias,source) VALUES(?,?,?)",
        (person_id, alias, source),
    )


def add_alias(public_id: str, alias: str, *, source: str = "manual", conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or connect()
    try:
        person = get_person(public_id, conn)
        if not person:
            raise KeyError(public_id)
        with _txn(conn):
            _ensure_alias(conn, person["person_id"], alias.strip(), source)
    finally:
        if own:
            conn.close()


def merge_person(from_public_id: str, into_public_id: str, conn: sqlite3.Connection | None = None) -> None:
    """把 from 并入 into：迁移别名、去重 facts/person_assets/library_links 引用，
    from 标 archived 且 merged_into=into（旧 public_id 保留作重定向）。"""
    own = conn is None
    conn = conn or connect()
    try:
        src = get_person(from_public_id, conn)
        dst = get_person(into_public_id, conn)
        if not src or not dst:
            raise KeyError("both persons must exist to merge")
        if src["person_id"] == dst["person_id"]:
            return
        sid, did = src["person_id"], dst["person_id"]
        ts = now_utc()
        with _txn(conn):
            # 别名迁移（冲突忽略），并把旧主名也留作 into 的别名以便检索。
            conn.execute(
                "INSERT OR IGNORE INTO person_aliases(person_id,alias,source) "
                "SELECT ?,alias,source FROM person_aliases WHERE person_id=?",
                (did, sid),
            )
            conn.execute(
                "INSERT OR IGNORE INTO person_aliases(person_id,alias,source) VALUES(?,?,?)",
                (did, src["display_name"], "merge"),
            )
            # 子表引用改挂到 into（去重由唯一/主键约束兜底）。
            conn.execute(
                "INSERT OR IGNORE INTO facts(person_id,ordinal,statement,source,source_ref,"
                "verified,human_confirmed,edited_by_user) "
                "SELECT ?,ordinal,statement,source,source_ref,verified,human_confirmed,edited_by_user "
                "FROM facts WHERE person_id=?",
                (did, sid),
            )
            conn.execute("DELETE FROM facts WHERE person_id=?", (sid,))
            conn.execute(
                "INSERT OR IGNORE INTO person_assets(person_id,asset_id,state,is_cover,role) "
                "SELECT ?,asset_id,state,is_cover,role FROM person_assets WHERE person_id=?",
                (did, sid),
            )
            conn.execute("DELETE FROM person_assets WHERE person_id=?", (sid,))
            conn.execute(
                "INSERT OR IGNORE INTO library_links(person_id,doc_id,book_id,role,source_title,source_page) "
                "SELECT ?,doc_id,book_id,role,source_title,source_page FROM library_links WHERE person_id=?",
                (did, sid),
            )
            conn.execute("DELETE FROM library_links WHERE person_id=?", (sid,))
            conn.execute(
                "INSERT OR IGNORE INTO group_members(group_name,person_id,added_at) "
                "SELECT group_name,?,added_at FROM group_members WHERE person_id=?",
                (did, sid),
            )
            conn.execute("DELETE FROM group_members WHERE person_id=?", (sid,))
            conn.execute(
                "UPDATE persons SET state='archived', merged_into=?, archived_at=?, updated_at=? "
                "WHERE person_id=?",
                (into_public_id, ts, ts, sid),
            )
    finally:
        if own:
            conn.close()


# ── Fact / Asset / PersonAsset ──────────────────────────────────────────
def add_fact(person_public_id: str, statement: str, *, ordinal: int = 0, source: str = "manual",
             verified: int = 0, human_confirmed: int = 0, edited_by_user: int = 0,
             source_ref: str = "", conn: sqlite3.Connection | None = None) -> int:
    own = conn is None
    conn = conn or connect()
    try:
        person = get_person(person_public_id, conn)
        if not person:
            raise KeyError(person_public_id)
        with _txn(conn):
            cur = conn.execute(
                "INSERT INTO facts(person_id,ordinal,statement,source,source_ref,"
                "verified,human_confirmed,edited_by_user) VALUES(?,?,?,?,?,?,?,?)",
                (person["person_id"], ordinal, statement, source, source_ref,
                 verified, human_confirmed, edited_by_user),
            )
            return int(cur.lastrowid)
    finally:
        if own:
            conn.close()


def upsert_asset(conn: sqlite3.Connection, *, kind: str, path: str, content_hash: str,
                 size_bytes: int = 0, provider: str = "", license: str = "", license_url: str = "",
                 author: str = "", source_page: str = "", source_title: str = "",
                 meta: dict | None = None) -> int:
    """按 (content_hash, kind) 去重登记文件实体；存在则复用并返回 asset_id。"""
    row = conn.execute(
        "SELECT asset_id FROM assets WHERE content_hash=? AND kind=?",
        (content_hash, kind),
    ).fetchone()
    if row:
        return int(row["asset_id"])
    cur = conn.execute(
        "INSERT INTO assets(kind,path,content_hash,size_bytes,provider,license,license_url,"
        "author,source_page,source_title,state,meta_json,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?, 'active', ?, ?)",
        (kind, path, content_hash, size_bytes, provider, license, license_url, author,
         source_page, source_title, json.dumps(meta or {}, ensure_ascii=False), now_utc()),
    )
    return int(cur.lastrowid)


def link_person_asset(conn: sqlite3.Connection, person_public_id: str, asset_id: int, *,
                      state: str = "pending", is_cover: bool = False, role: str = "") -> None:
    person = get_person(person_public_id, conn)
    if not person:
        raise KeyError(person_public_id)
    conn.execute(
        "INSERT OR IGNORE INTO person_assets(person_id,asset_id,state,is_cover,role) "
        "VALUES(?,?,?,?,?)",
        (person["person_id"], asset_id, state, 1 if is_cover else 0, role),
    )


_ASSET_STATES = {"pending", "approved", "excluded"}


def _person_id_by_name(display_name: str, conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT person_id FROM persons WHERE display_name=? AND state='active' LIMIT 1",
        ((display_name or "").strip(),),
    ).fetchone()
    return int(row["person_id"]) if row else None


def set_person_asset_cover(display_name: str, asset_id: int, conn: sqlite3.Connection | None = None) -> bool:
    """把指定素材设为人物封面（每人唯一封面：其余清零）。

    仅对已关联到该人物的生效素材有效，未关联返回 False（调用方转 404）。
    """
    own = conn is None
    conn = conn or connect()
    try:
        pid = _person_id_by_name(display_name, conn)
        if pid is None:
            return False
        linked = conn.execute(
            "SELECT 1 FROM person_assets WHERE person_id=? AND asset_id=?",
            (pid, asset_id),
        ).fetchone()
        if not linked:
            return False
        with _txn(conn):
            conn.execute("UPDATE person_assets SET is_cover=0 WHERE person_id=?", (pid,))
            conn.execute(
                "UPDATE person_assets SET is_cover=1 WHERE person_id=? AND asset_id=?",
                (pid, asset_id),
            )
        return True
    finally:
        if own:
            conn.close()


def set_person_asset_state(display_name: str, asset_id: int, state: str, conn: sqlite3.Connection | None = None) -> bool:
    """更新人物级素材状态；excluded 即人物级黑名单（里程碑 4 会在检索侧强制执行）。

    只改 person_assets.state（人物维度），不动 assets.state（全局文件实体），
    同一图片对其他人物不受影响。非法状态名抛 ValueError。
    """
    if state not in _ASSET_STATES:
        raise ValueError(f"invalid asset state: {state}")
    own = conn is None
    conn = conn or connect()
    try:
        pid = _person_id_by_name(display_name, conn)
        if pid is None:
            return False
        with _txn(conn):
            cur = conn.execute(
                "UPDATE person_assets SET state=? WHERE person_id=? AND asset_id=?",
                (state, pid, asset_id),
            )
            # 排除时顺手取消封面，避免"被排除的图仍是封面"的矛盾态。
            if state == "excluded" and cur.rowcount:
                conn.execute(
                    "UPDATE person_assets SET is_cover=0 WHERE person_id=? AND asset_id=?",
                    (pid, asset_id),
                )
        return cur.rowcount > 0
    finally:
        if own:
            conn.close()


# ── 删除 / 对账 ──────────────────────────────────────────────────────────
def soft_delete_person(public_id: str, conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or connect()
    try:
        ts = now_utc()
        with _txn(conn):
            conn.execute(
                "UPDATE persons SET state='archived', archived_at=?, updated_at=? WHERE public_id=?",
                (ts, ts, public_id),
            )
    finally:
        if own:
            conn.close()


def hard_delete_person(public_id: str, *, storage_root: str, conn: sqlite3.Connection | None = None) -> list[str]:
    """二次确认后硬删：事务内删库行（子表 CASCADE），提交后删除**仅本人引用**的文件。
    返回被删文件的相对路径列表。崩溃遗留的孤儿由 reconcile 兜底。"""
    own = conn is None
    conn = conn or connect()
    orphan_files: list[str] = []
    try:
        person = get_person(public_id, conn)
        if not person:
            return []
        pid = person["person_id"]
        # 找出仅被该人物引用（删 CASCADE 后失去所有引用）的素材文件。
        rows = conn.execute(
            """SELECT a.asset_id, a.path FROM assets a
               JOIN person_assets pa ON pa.asset_id = a.asset_id
               WHERE pa.person_id=?
                 AND (SELECT COUNT(*) FROM person_assets WHERE asset_id=a.asset_id) = 1""",
            (pid,),
        ).fetchall()
        orphan_ids = [int(r["asset_id"]) for r in rows]
        orphan_files = [r["path"] for r in rows]
        with _txn(conn):
            # 孤儿 assets 行一并删除（assets 行去 → 其残余 person_assets 由 CASCADE 清）。
            conn.executemany(
                "DELETE FROM assets WHERE asset_id=?", [(i,) for i in orphan_ids]
            )
            conn.execute("DELETE FROM persons WHERE person_id=?", (pid,))
        # 提交后再删文件（事务已释放）。
        for rel in orphan_files:
            _safe_remove(storage_root, rel)
    finally:
        if own:
            conn.close()
    return orphan_files


def _safe_remove(storage_root: str, rel_path: str) -> None:
    root = os.path.abspath(storage_root) + os.sep
    full = os.path.abspath(os.path.join(storage_root, rel_path))
    if not full.startswith(root):  # 防越界
        return
    try:
        if os.path.isfile(full):
            os.remove(full)
    except OSError:
        pass


def reconcile(conn: sqlite3.Connection, *, storage_root: str, apply: bool = False) -> dict[str, Any]:
    """扫描 assets/videos 的文件可达性：缺失标 file_missing（apply=True 才写库）；
    返回报告 {missing:[...], marked:[...], orphans:[...]}。只读默认，安全。"""
    missing: list[str] = []
    marked: list[str] = []
    to_mark: list[tuple[str, str, int, str]] = []  # (table, idcol, id, path)
    for table, col in (("assets", "asset_id"), ("videos", "video_id")):
        rows = conn.execute(f"SELECT {col} AS id, path, state FROM {table}").fetchall()
        for r in rows:
            exists = os.path.isfile(os.path.join(storage_root, r["path"]))
            if not exists and r["state"] != "file_missing":
                missing.append(r["path"])
                to_mark.append((table, col, int(r["id"]), r["path"]))
    if apply:
        # 先读完再写，避免遍历游标的同时改同表；写走 BEGIN IMMEDIATE 短事务。
        with _txn(conn):
            for table, col, idv, path in to_mark:
                conn.execute(
                    f"UPDATE {table} SET state='file_missing' WHERE {col}=?", (idv,)
                )
                marked.append(path)
    # 孤儿：磁盘 tasks 下无库引用的文件（仅报告，删由人工/清理任务经 person_repo）。
    referenced = {r["path"] for r in conn.execute("SELECT path FROM assets")}
    referenced |= {r["path"] for r in conn.execute("SELECT path FROM videos")}
    orphans: list[str] = []
    tasks_root = os.path.join(storage_root, "tasks")
    if os.path.isdir(tasks_root):
        for dirpath, _dirs, files in os.walk(tasks_root):
            for f in files:
                rel = os.path.relpath(os.path.join(dirpath, f), storage_root).replace("\\", "/")
                if rel not in referenced:
                    orphans.append(rel)
    return {"missing": missing, "marked": marked, "orphans": orphans}
