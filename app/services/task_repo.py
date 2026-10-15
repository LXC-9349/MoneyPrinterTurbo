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

from app.services.person_repo import connect, create_or_get_person, now_utc


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

# 终态：已确定结果、可安全归档/删除/重试的状态。活动态（排队/运行/待确认）
# 与草稿均不在此列——它们需先取消才能删除，不能直接归档。
TERMINAL_STATUSES = ("completed", "failed", "cancelled")


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


def _person_id_for(conn: sqlite3.Connection, person_name: str) -> int | None:
    """提交任务时解析人物：mpt persons 不存在则自动物化建档（里程碑 2 收尾 #7）。

    与 facts/别名端点同一建档模式（create_or_get_person 幂等，主名也入别名表）；
    建档失败不阻断登记，返回 None 由调用方降级为无 person_id 行。
    """
    name = (person_name or "").strip()
    if not name:
        return None
    try:
        return int(create_or_get_person(name, conn=conn)["person_id"])
    except Exception:  # pragma: no cover - 旁路建档失败不阻断任务登记
        return None


def record_task(
    task_id: str,
    *,
    person_name: str = "",
    title: str = "",
    stop_at: str = "video",
    params: dict | None = None,
    parent_task_id: str = "",
    version: int = 1,
    conn: sqlite3.Connection | None = None,
) -> None:
    """提交生成时登记一行任务（status=processing）。幂等：已存在则不覆盖进度。

    重试产生的新任务带上 parent_task_id 与原任务 version+1（版本链）；首次
    提交默认 parent 为空、version=1。
    """
    own = conn is None
    conn = conn or connect()
    ts = now_utc()
    pid = _person_id_for(conn, person_name)
    try:
        with _txn(conn):
            conn.execute(
                """INSERT INTO tasks(task_id,person_id,person_name,title,stop_at,status,progress,
                       params_json,parent_task_id,version,created_at,started_at,updated_at)
                   VALUES(?,?,?,?,?,'processing',0,?,?,?,?,?,?)
                   ON CONFLICT(task_id) DO NOTHING""",
                (
                    task_id,
                    pid,
                    person_name,
                    title,
                    stop_at,
                    json.dumps(params or {}, ensure_ascii=False),
                    parent_task_id or "",
                    int(version or 1),
                    ts,
                    ts,
                    ts,
                ),
            )
    finally:
        if own:
            conn.close()


def save_draft(
    task_id: str,
    *,
    person_name: str = "",
    title: str = "",
    stop_at: str = "video",
    params: dict | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """存为草稿：以参数快照登记/覆盖一行 status='draft'（不启动生成）。

    与 record_task 的区别：草稿不计入“进行中”集合（ACTIVE_STATUSES 无 draft），
    可反复覆盖 params_json 直到用户“创建并开始”。ON CONFLICT 覆盖草稿内容。
    """
    own = conn is None
    conn = conn or connect()
    ts = now_utc()
    pid = _person_id_for(conn, person_name)
    try:
        with _txn(conn):
            conn.execute(
                """INSERT INTO tasks(task_id,person_id,person_name,title,stop_at,status,progress,
                       params_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,'draft',0,?,?,?)
                   ON CONFLICT(task_id) DO UPDATE SET
                       person_id=excluded.person_id,
                       person_name=excluded.person_name, title=excluded.title,
                       stop_at=excluded.stop_at, status='draft', progress=0,
                       params_json=excluded.params_json, updated_at=excluded.updated_at""",
                (
                    task_id,
                    pid,
                    person_name,
                    title,
                    stop_at,
                    json.dumps(params or {}, ensure_ascii=False),
                    ts,
                    ts,
                ),
            )
    finally:
        if own:
            conn.close()


def delete_draft(task_id: str, conn: sqlite3.Connection | None = None) -> bool:
    """删除草稿行（仅限 draft 状态，绝不动已调度/终态任务）。返回是否命中。"""
    own = conn is None
    conn = conn or connect()
    try:
        with _txn(conn):
            cur = conn.execute(
                "DELETE FROM tasks WHERE task_id=? AND status='draft'", (task_id,)
            )
            return cur.rowcount > 0
    finally:
        if own:
            conn.close()


def backfill_orphan_persons(conn: sqlite3.Connection | None = None) -> int:
    """迁移补档（里程碑 2 收尾 #7）：为历史孤儿任务补建人物并回填 tasks.person_id。

    与提交时同一建档路径（_person_id_for → create_or_get_person，幂等）；还同步
    可见性建档（文库/personal.sqlite 都没有的按自建），让这些任务能在人物卡片上
    看到。重复执行只处理剩余孤儿。返回本次补建的人物数。
    """
    own = conn is None
    conn = conn or connect()
    names: list[str] = []
    try:
        rows = conn.execute(
            """SELECT DISTINCT person_name FROM tasks
               WHERE person_name IS NOT NULL AND person_name<>''
                 AND person_name NOT IN (
                   SELECT display_name FROM persons WHERE state='active')"""
        ).fetchall()
        names = [r["person_name"] for r in rows]
        for name in names:
            pid = _person_id_for(conn, name)
            if pid is None:
                continue
            with _txn(conn):
                conn.execute(
                    "UPDATE tasks SET person_id=? WHERE person_name=? AND person_id IS NULL",
                    (pid, name),
                )
    finally:
        if own:
            conn.close()
    # 可见性建档旁路：文库里的人（曹操等）本就在卡片上，不需要 person_meta 行。
    try:
        from app.services.person_store import ensure_person_for_task

        for name in names:
            ensure_person_for_task(name)
    except Exception:  # pragma: no cover - 叠加层异常不阻断补档
        pass
    return len(names)


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
                   updated_at=? WHERE task_id=? AND status != 'cancelled'""",
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


def get_lineage(task_id: str, conn: sqlite3.Connection | None = None) -> list[dict]:
    """返回任务所属版本链的全部行（祖先 + 自身 + 后代），按 version、created_at 升序。

    从自身沿 parent_task_id 上溯到根，再从根向下 BFS 收集所有后代，覆盖重试
    形成的分叉。任务行不存在时返回空列表。供任务详情页“版本切换”使用。
    """
    own = conn is None
    conn = conn or connect()
    try:
        cur = conn.execute(
            "SELECT parent_task_id FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if cur is None:
            return []
        # 上溯到根（防御环：seen 去重）。
        root = task_id
        seen_up = {task_id}
        parent = cur["parent_task_id"]
        while parent and parent not in seen_up:
            seen_up.add(parent)
            root = parent
            cur = conn.execute(
                "SELECT parent_task_id FROM tasks WHERE task_id=?", (parent,)
            ).fetchone()
            if cur is None:
                break
            parent = cur["parent_task_id"]
        # 从根向下 BFS 收集全部后代。
        ids = {root}
        frontier = [root]
        while frontier:
            marks = ",".join("?" * len(frontier))
            rows = conn.execute(
                f"SELECT task_id FROM tasks WHERE parent_task_id IN ({marks})", frontier
            ).fetchall()
            nxt = [r["task_id"] for r in rows if r["task_id"] not in ids]
            ids.update(nxt)
            frontier = nxt
        marks = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT task_id,title,person_name,status,version,created_at,completed_at,"
            f"parent_task_id,archived FROM tasks WHERE task_id IN ({marks}) "
            f"ORDER BY version ASC, created_at ASC",
            list(ids),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


def list_tasks(
    *,
    status: str = "",
    person_name: str = "",
    keyword: str = "",
    archived: int | None = 0,
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
    # archived：0=仅未归档（默认，归档项从主列表隐藏），1=仅归档，None=不分。
    if archived is not None:
        sql += " AND archived=?"
        args.append(int(archived))
    sql += " ORDER BY (status IN " + _ACTIVE_SQL + ") DESC, created_at DESC LIMIT ?"
    args.append(int(limit))
    try:
        rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


def active_tasks(conn: sqlite3.Connection | None = None) -> list[dict]:
    return list_tasks(conn=conn, limit=50)  # 排序已把进行中/排队置顶；调用方按需过滤


def mark_cancelled(task_id: str, conn: sqlite3.Connection | None = None) -> bool:
    """把任务标为已取消（抽屉“取消”按钮）。仅对活动态行生效；返回是否命中。

    硬中断（停下正在跑的阶段）属里程碑 3；里程碑 1 先把任务从进行中抽屉移除
    并固定为 cancelled 终态，避免前端重复展示。
    """
    own = conn is None
    conn = conn or connect()
    ts = now_utc()
    try:
        with _txn(conn):
            cur = conn.execute(
                """UPDATE tasks SET status='cancelled', completed_at=COALESCE(completed_at, ?),
                       updated_at=? WHERE task_id=? AND status IN """ + _ACTIVE_SQL,
                (ts, ts, task_id),
            )
            return cur.rowcount > 0
    finally:
        if own:
            conn.close()


def set_compliance_ack(
    task_id: str,
    *,
    acknowledged: bool = True,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """记录/撤销合规“我已知晓”确认（里程碑 3 #7）。

    acknowledged=True 写入当前 UTC 时刻到 compliance_ack_at（解禁导出）；False
    清回 NULL（重新需要确认）。仅更新该列与 updated_at，不动状态。返回是否命中行。
    """
    own = conn is None
    conn = conn or connect()
    ts = now_utc()
    try:
        with _txn(conn):
            cur = conn.execute(
                "UPDATE tasks SET compliance_ack_at=?, updated_at=? WHERE task_id=?",
                (ts if acknowledged else None, ts, task_id),
            )
            return cur.rowcount > 0
    finally:
        if own:
            conn.close()


def archive_tasks(task_ids: list[str], conn: sqlite3.Connection | None = None) -> list[str]:
    """批量归档：仅对终态且未归档的行置 archived=1，返回命中的 task_id。

    与 status 正交（不改原状态）；活动态/草稿不可归档（需先取消），故
    WHERE 限定 status IN 终态。重复归档幂等（已 archived=1 不再命中）。
    """
    ids = [i for i in (task_ids or []) if i]
    if not ids:
        return []
    own = conn is None
    conn = conn or connect()
    ts = now_utc()
    placeholders = ",".join("?" * len(ids))
    terminal = "(" + ",".join(f"'{s}'" for s in TERMINAL_STATUSES) + ")"
    try:
        with _txn(conn):
            rows = conn.execute(
                f"SELECT task_id FROM tasks WHERE task_id IN ({placeholders}) "
                f"AND status IN {terminal} AND archived=0",
                ids,
            ).fetchall()
            hit = [r["task_id"] for r in rows]
            if hit:
                marks = ",".join("?" * len(hit))
                conn.execute(
                    f"UPDATE tasks SET archived=1, archived_at=?, updated_at=? "
                    f"WHERE task_id IN ({marks})",
                    [ts, ts, *hit],
                )
            return hit
    finally:
        if own:
            conn.close()


def unarchive_tasks(task_ids: list[str], conn: sqlite3.Connection | None = None) -> list[str]:
    """批量取消归档：置 archived=0/archived_at=NULL，返回命中的 task_id。"""
    ids = [i for i in (task_ids or []) if i]
    if not ids:
        return []
    own = conn is None
    conn = conn or connect()
    ts = now_utc()
    placeholders = ",".join("?" * len(ids))
    try:
        with _txn(conn):
            rows = conn.execute(
                f"SELECT task_id FROM tasks WHERE task_id IN ({placeholders}) AND archived=1",
                ids,
            ).fetchall()
            hit = [r["task_id"] for r in rows]
            if hit:
                marks = ",".join("?" * len(hit))
                conn.execute(
                    f"UPDATE tasks SET archived=0, archived_at=NULL, updated_at=? "
                    f"WHERE task_id IN ({marks})",
                    [ts, *hit],
                )
            return hit
    finally:
        if own:
            conn.close()


def reset_for_resume(task_id: str, conn: sqlite3.Connection | None = None) -> bool:
    """从失败阶段继续（里程碑 3 #4）：把 failed/cancelled 行重置为 processing。

    清 error/failed_stage/completed_at 与旧阶段耗时（重新计时），保留 params_json 与
    任务目录里已生成的中间产物（脚本/缓存素材/配音/字幕），使流水线重跑时能复用它们、
    只重做失败阶段。仅对 failed/cancelled 终态行生效；返回是否命中。

    先重置状态也是为了绕过 update_from_state 的 `status != 'cancelled'` 守卫——
    否则被取消的任务重新派发后回写不进去。
    """
    own = conn is None
    conn = conn or connect()
    ts = now_utc()
    try:
        with _txn(conn):
            cur = conn.execute(
                "UPDATE tasks SET status='processing', progress=0, stage='', error='', "
                "failed_stage='', stage_timings='{}', completed_at=NULL, updated_at=? "
                "WHERE task_id=? AND status IN ('failed','cancelled','interrupted')",
                (ts, task_id),
            )
            return cur.rowcount > 0
    finally:
        if own:
            conn.close()


def mark_interrupted_on_startup(
    conn: sqlite3.Connection | None = None,
) -> int:
    """服务重启收敛遗留活动任务（里程碑 3 #5）：把上次进程未跑完的任务标为 interrupted。

    内存态队列不跨进程恢复：新进程启动时队列为空，DB 里仍处于排队/运行/待确认的行
    一定是上次进程遗留、已无人推进的“孤儿”，标记为 interrupted（可续跑）并从进行中抽屉
    移除。保留 stage/params_json 以便详情页展示中断点并从失败阶段继续。返回命中行数。

    仅用于内存态调度（单进程）；Redis 多进程部署由 resume_queued_tasks 重新接管，
    调用方须自行避免误标其它进程正在执行的任务。
    """
    own = conn is None
    conn = conn or connect()
    ts = now_utc()
    try:
        with _txn(conn):
            cur = conn.execute(
                "UPDATE tasks SET status='interrupted', "
                "error=CASE WHEN error='' THEN '服务重启导致任务中断' ELSE error END, "
                "updated_at=? WHERE status IN " + _ACTIVE_SQL,
                (ts,),
            )
            return cur.rowcount
    finally:
        if own:
            conn.close()


def delete_tasks(task_ids: list[str], conn: sqlite3.Connection | None = None) -> list[str]:
    """批量删除任务行：仅删非活动态行（运行中/排队/待确认绝不误删），返回命中的 id。

    只删登记行，磁盘产物由调用方（控制器）决定是否一并清理——repo 不碰文件系统。
    """
    ids = [i for i in (task_ids or []) if i]
    if not ids:
        return []
    own = conn is None
    conn = conn or connect()
    placeholders = ",".join("?" * len(ids))
    try:
        with _txn(conn):
            rows = conn.execute(
                f"SELECT task_id FROM tasks WHERE task_id IN ({placeholders}) "
                f"AND status NOT IN {_ACTIVE_SQL}",
                ids,
            ).fetchall()
            hit = [r["task_id"] for r in rows]
            if hit:
                marks = ",".join("?" * len(hit))
                conn.execute(f"DELETE FROM tasks WHERE task_id IN ({marks})", hit)
            return hit
    finally:
        if own:
            conn.close()


def record_stage_timing(
    task_id: str,
    stage: str,
    seconds: float,
    *,
    conn: sqlite3.Connection | None = None,
) -> None:
    """增量合并写 tasks.stage_timings（里程碑 3 #1 阶段耗时）。

    读-改-写 JSON map：{stage: 秒}，同阶段重复写以新值覆盖。只在阶段边界
    触发（每个任务个位数次数），行级短事务足够。行不存在或
    stage 为空时静默跳过，任何异常不得外溢——计时是观测能力，不阻断生成。
    """
    stage = (stage or "").strip()
    if not stage:
        return
    try:
        value = round(float(seconds), 2)
    except (TypeError, ValueError):
        return
    own = conn is None
    conn = conn or connect()
    ts = now_utc()
    try:
        with _txn(conn):
            row = conn.execute(
                "SELECT stage_timings FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                return
            try:
                timings = json.loads(row["stage_timings"] or "{}")
            except (ValueError, TypeError):
                timings = {}
            if not isinstance(timings, dict):
                timings = {}
            timings[stage] = value
            conn.execute(
                "UPDATE tasks SET stage_timings=?, updated_at=? WHERE task_id=?",
                (json.dumps(timings, ensure_ascii=False), ts, task_id),
            )
    finally:
        if own:
            conn.close()


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
                      COUNT(*)                          AS task_count,
                      SUM(status='completed')        AS video_count,
                      SUM(status IN """ + _ACTIVE_SQL + """)          AS active_count,
                      SUM(status='awaiting_confirmation') AS awaiting_count,
                      MAX(updated_at)                AS last_updated
               FROM tasks WHERE person_name != ''
               GROUP BY person_name"""
        ).fetchall():
            stats[r["person_name"]] = {
                "task_count": int(r["task_count"] or 0),
                "video_count": int(r["video_count"] or 0),
                "article_count": 0,
                "article_enabled_count": 0,
                "active_task_count": int(r["active_count"] or 0),
                "awaiting_count": int(r["awaiting_count"] or 0),
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
                    "task_count": 0,
                    "video_count": 0,
                    "article_count": 0,
                    "article_enabled_count": 0,
                    "active_task_count": 0,
                    "awaiting_count": 0,
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
