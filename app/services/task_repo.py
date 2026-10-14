"""任务聚合根数据访问层（里程碑 1）。

职责：
* 唯一持有 `mpt.sqlite` 里 `tasks` 表的读写，连接来自 `db_migrations.connect`
  （强制外键 / WAL，schema 自动前滚）。沿用 person_repo 的单向分层与
  `BEGIN IMMEDIATE` 短事务约定，时间统一 UTC ISO8601（尾部 Z）。
* 提供人物卡片的聚合统计：一次 `GROUP BY` SQL 得到 {person_name: counts}，
  由上层合并到人物列表，避免逐个查询的 N+1。
* 幂等迁移既有 `projects.json`（草稿 / 已完成）为任务行，不丢数据。

本模块只做数据底座与统计，不改现有生成链路的调用路径。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from app.services.person_repo import connect, now_utc


class _txn:
    """BEGIN IMMEDIATE 短事务（与 person_repo 同一语义，不跨线程共享连接）。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False

# const 里的整数状态码 → 任务表 status 文本。集中在此映射，避免各处硬编码。
_STATE_TO_STATUS = {
    -1: "failed",
    1: "completed",
    2: "queued",
    3: "queued",
    4: "processing",
    5: "cancelled",
}

# “进行中”的单一事实源：排队 / 运行中 / 分阶段确认等待。“草稿”（draft）与终态
# 均不计入——迁移自 projects.json 的历史草稿不是活动任务，不应占用顶栏“进行中”抽屉。
ACTIVE_STATUSES = ("queued", "processing", "awaiting_confirmation")
_ACTIVE_SQL = "(" + ",".join(f"'{s}'" for s in ACTIVE_STATUSES) + ")"


def _iso_from_epoch(value: Any) -> str | None:
    """把 projects.json 里的 float epoch 秒转成 UTC ISO8601（尾部 Z）。"""
    if not value:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (TypeError, ValueError, OSError):
        return None


def record_task(
    task_id: str,
    *,
    person_name: str = "",
    title: str = "",
    stop_at: str = "video",
    params: dict | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """提交生成时登记一行任务（status=processing）。幂等：已存在则不覆盖进度。"""
    own = conn is None
    conn = conn or connect()
    ts = now_utc()
    try:
        with _txn(conn):
            conn.execute(
                """INSERT INTO tasks(task_id,person_name,title,stop_at,status,progress,
                       params_json,created_at,started_at,updated_at)
                   VALUES(?,?,?,?,'processing',0,?,?,?,?)
                   ON CONFLICT(task_id) DO NOTHING""",
                (
                    task_id,
                    person_name,
                    title,
                    stop_at,
                    json.dumps(params or {}, ensure_ascii=False),
                    ts,
                    ts,
                    ts,
                ),
            )
    finally:
        if own:
            conn.close()


def update_from_state(
    task_id: str,
    state: dict,
    *,
    conn: sqlite3.Connection | None = None,
) -> None:
    """用任务状态字典（sm.state / API 返回）刷新任务行的进度、阶段与终态。"""
    own = conn is None
    conn = conn or connect()
    ts = now_utc()
    raw_state = state.get("state")
    try:
        status = _STATE_TO_STATUS.get(int(raw_state), "processing")  # type: ignore[arg-type]
    except (TypeError, ValueError):
        status = "processing"
    with _txn(conn):
        conn.execute(
            """UPDATE tasks SET status=?, stage=?, progress=?, error=?, failed_stage=?,
                   warnings_json=?, artifacts_json=?,
                   completed_at=CASE WHEN ? IN ('completed','failed','cancelled')
                       THEN COALESCE(completed_at, ?) ELSE completed_at END,
                   updated_at=? WHERE task_id=?""",
            (
                status,
                str(state.get("stage") or state.get("failed_stage") or ""),
                int(state.get("progress") or 0),
                str(state.get("error") or ""),
                str(state.get("failed_stage") or ""),
                json.dumps(state.get("warnings") or [], ensure_ascii=False),
                json.dumps(
                    {
                        "videos": state.get("videos") or [],
                        "combined_videos": state.get("combined_videos") or [],
                        "audio_file": state.get("audio_file") or "",
                        "subtitle_path": state.get("subtitle_path") or "",
                    },
                    ensure_ascii=False,
                ),
                status,
                ts,
                ts,
                task_id,
            ),
        )
    if own:
        conn.close()


def get_task(task_id: str, conn: sqlite3.Connection | None = None) -> dict | None:
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute(
            "SELECT * FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        if own:
            conn.close()


def list_tasks(
    *,
    status: str = "",
    person_name: str = "",
    keyword: str = "",
    limit: int = 200,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    own = conn is None
    conn = conn or connect()
    sql = "SELECT * FROM tasks WHERE 1=1"
    args: list[Any] = []
    if status:
        sql += " AND status=?"
        args.append(status)
    if person_name:
        sql += " AND person_name=?"
        args.append(person_name)
    if keyword:
        sql += " AND (title LIKE ? OR person_name LIKE ?)"
        args += [f"%{keyword}%", f"%{keyword}%"]
    sql += " ORDER BY (status IN ('processing','queued','pending')) DESC, created_at DESC LIMIT ?"
    args.append(int(limit))
    try:
        rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


def active_tasks(conn: sqlite3.Connection | None = None) -> list[dict]:
    return list_tasks(conn=conn, limit=50)  # 排序已把进行中/排队置顶；调用方按需过滤


def counts_by_person(conn: sqlite3.Connection | None = None) -> dict[str, dict[str, Any]]:
    """一次聚合返回 {person_name: 统计}，供人物卡片直接使用（杜绝 N+1）。

    只发两条常量级 `GROUP BY`（tasks + articles），与人物数量无关。
    """
    own = conn is None
    conn = conn or connect()
    try:
        stats: dict[str, dict[str, Any]] = {}
        for r in conn.execute(
            """SELECT person_name,
                      SUM(status='completed')        AS video_count,
                      SUM(status IN """ + _ACTIVE_SQL + """)          AS active_count,
                      MAX(updated_at)                AS last_updated
               FROM tasks WHERE person_name != ''
               GROUP BY person_name"""
        ).fetchall():
            stats[r["person_name"]] = {
                "video_count": int(r["video_count"] or 0),
                "article_count": 0,
                "article_enabled_count": 0,
                "active_task_count": int(r["active_count"] or 0),
                "last_updated": r["last_updated"],
            }
        for r in conn.execute(
            """SELECT person_name,
                      COUNT(*)                    AS total,
                      SUM(enabled)                AS on_count,
                      MAX(updated_at)             AS last_updated
               FROM articles WHERE person_name != ''
               GROUP BY person_name"""
        ).fetchall():
            entry = stats.setdefault(
                r["person_name"],
                {
                    "video_count": 0,
                    "article_count": 0,
                    "article_enabled_count": 0,
                    "active_task_count": 0,
                    "last_updated": None,
                },
            )
            entry["article_count"] = int(r["total"] or 0)
            entry["article_enabled_count"] = int(r["on_count"] or 0)
            if r["last_updated"] and (
                not entry["last_updated"] or r["last_updated"] > entry["last_updated"]
            ):
                entry["last_updated"] = r["last_updated"]
        return stats
    finally:
        if own:
            conn.close()


def migrate_projects(conn: sqlite3.Connection | None = None) -> int:
    """把 projects.json 的历史记录迁移为任务行（幂等，按 task_id / 项目 id）。

    返回本次新增行数。已存在（ON CONFLICT DO NOTHING）的不重复插入。无
    task_id 的草稿用 ``draft_<project_id>`` 作合成主键，保证“草稿也能迁移、
    不丢数据”。
    """
    from app.services import project_store

    own = conn is None
    conn = conn or connect()
    added = 0
    try:
        with _txn(conn):
            for entry in project_store.list_projects():
                pid = entry.get("id", "")
                task_id = entry.get("task_id", "") or f"draft_{pid}"
                person_name = (
                    entry.get("person")
                    or (entry.get("draft") or {}).get("biography_person")
                    or (entry.get("draft") or {}).get("video_subject")
                    or ""
                )
                status = {
                    "draft": "draft",
                    "processing": "processing",
                    "done": "completed",
                    "failed": "failed",
                }.get(entry.get("status", "draft"), "draft")
                cur = conn.execute(
                    """INSERT INTO tasks(task_id,person_name,title,stop_at,status,progress,
                           params_json,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,? ,?)
                       ON CONFLICT(task_id) DO NOTHING""",
                    (
                        task_id,
                        person_name,
                        entry.get("title", ""),
                        "video",
                        status,
                        100 if status == "completed" else 0,
                        json.dumps(entry.get("draft") or {}, ensure_ascii=False),
                        _iso_from_epoch(entry.get("created_at")) or now_utc(),
                        _iso_from_epoch(entry.get("updated_at")) or now_utc(),
                    ),
                )
                added += cur.rowcount if cur.rowcount > 0 else 0
    except (sqlite3.Error, OSError) as exc:
        logger.warning(f"projects→tasks migration failed: {type(exc).__name__}: {exc}")
    finally:
        if own:
            conn.close()
    return added
