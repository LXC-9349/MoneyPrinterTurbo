"""Schema 迁移框架（阶段 2 步骤 1，纯新增，未接线到现有流程）。

设计（见 docs/DESIGN_DATA_MODEL_AND_DEGRADATION.md §2.7/§2.8）：
* **单一真源**：`mpt.sqlite` 的建表/演进只在此定义。
* **schema 迁移只前滚、不写 down**：`PRAGMA user_version` 递增，启动/CLI 调
  `ensure_migrated()` 应用 `version > 当前` 的全部迁移。幂等、只加表/加列。
* **连接工厂统一 PRAGMA**：`foreign_keys=ON`（保证 `ON DELETE CASCADE` 生效）、
  WAL、`busy_timeout`、`synchronous=NORMAL`；连接**不跨线程共享**、写用
  `BEGIN IMMEDIATE` 短事务（在 person_repo 侧）。
* 一次性数据导入是**独立命令**（app/services/import_library.py），不在这里。

本文件不 import 其余 services，避免把分层依赖引入数据底座。
"""

from __future__ import annotations

import os
import sqlite3
import time

# ── 不透明对外 id ──────────────────────────────────────────────────────────
# Crockford Base32（去掉易混字符 I L O U）。ULID = 48bit 毫秒时间戳(10 字符)
# + 80bit 随机(16 字符)，共 26 字符；对外形态再加类型前缀，如 per_<ULID>。
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode_base32(value: int, length: int) -> str:
    out: list[str] = []
    for _ in range(length):
        out.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def new_ulid() -> str:
    ts = int(time.time() * 1000)
    rand = int.from_bytes(os.urandom(10), "big")  # 80 bit
    return _encode_base32(ts, 10) + _encode_base32(rand, 16)


def new_public_id(prefix: str) -> str:
    """生成带类型前缀的不透明 id，如 ``per_01J...``。"""
    return f"{prefix}_{new_ulid()}"


# ── 连接工厂 ───────────────────────────────────────────────────────────────
def connect(db_path: str) -> sqlite3.Connection:
    """建立带强制外键/WAL 的连接。连接不跨线程共享，用完即关。"""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # autocommit 模式：所有事务由调用方显式 BEGIN/COMMIT（配合 person_repo 的
    # BEGIN IMMEDIATE 短事务），避免 Python 隐式事务与手动 BEGIN 冲突。
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


# ── M0：完整建表（与 §2.2 一致）───────────────────────────────────────────
SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS persons (
  person_id     INTEGER PRIMARY KEY,
  public_id     TEXT NOT NULL UNIQUE,
  qid           TEXT NOT NULL DEFAULT '',
  display_name  TEXT NOT NULL,
  dynasty       TEXT NOT NULL DEFAULT '',
  category      TEXT NOT NULL DEFAULT '',
  note          TEXT NOT NULL DEFAULT '',
  pinned        INTEGER NOT NULL DEFAULT 0,
  origin        TEXT NOT NULL DEFAULT 'library',
  state         TEXT NOT NULL DEFAULT 'active',
  no_authentic_photo INTEGER NOT NULL DEFAULT 0,
  merged_into   TEXT NOT NULL DEFAULT '',
  archived_at   TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_person_state ON persons(state, pinned DESC, display_name);
CREATE INDEX IF NOT EXISTS idx_person_merged ON persons(merged_into);

CREATE TABLE IF NOT EXISTS person_aliases (
  person_id INTEGER NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
  alias     TEXT NOT NULL,
  source    TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (alias, person_id)
);
CREATE INDEX IF NOT EXISTS idx_alias_lookup ON person_aliases(alias);

CREATE TABLE IF NOT EXISTS facts (
  fact_id         INTEGER PRIMARY KEY,
  person_id       INTEGER NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
  ordinal         INTEGER NOT NULL,
  statement       TEXT NOT NULL,
  date_text       TEXT NOT NULL DEFAULT '',
  year_from       INTEGER,
  year_to         INTEGER,
  place           TEXT NOT NULL DEFAULT '',
  confidence      REAL NOT NULL DEFAULT 0,
  verify_reason   TEXT NOT NULL DEFAULT '',
  source          TEXT NOT NULL DEFAULT 'library',
  source_ref      TEXT NOT NULL DEFAULT '',
  verified        INTEGER NOT NULL DEFAULT 0,
  human_confirmed INTEGER NOT NULL DEFAULT 0,
  edited_by_user  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_facts_person ON facts(person_id, ordinal);

CREATE TABLE IF NOT EXISTS assets (
  asset_id      INTEGER PRIMARY KEY,
  kind          TEXT NOT NULL,
  path          TEXT NOT NULL,
  content_hash  TEXT NOT NULL,
  size_bytes    INTEGER NOT NULL DEFAULT 0,
  provider      TEXT NOT NULL DEFAULT '',
  license       TEXT NOT NULL DEFAULT '',
  license_url   TEXT NOT NULL DEFAULT '',
  author        TEXT NOT NULL DEFAULT '',
  source_page   TEXT NOT NULL DEFAULT '',
  source_title  TEXT NOT NULL DEFAULT '',
  state         TEXT NOT NULL DEFAULT 'active',
  meta_json     TEXT NOT NULL DEFAULT '{}',
  created_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_assets_hash ON assets(content_hash, kind);
CREATE INDEX IF NOT EXISTS idx_assets_state ON assets(state);

CREATE TABLE IF NOT EXISTS person_assets (
  person_id INTEGER NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
  asset_id  INTEGER NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
  state     TEXT NOT NULL DEFAULT 'pending',
  is_cover  INTEGER NOT NULL DEFAULT 0,
  role      TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (person_id, asset_id)
);
CREATE INDEX IF NOT EXISTS idx_pa_person_state ON person_assets(person_id, state);

CREATE TABLE IF NOT EXISTS scripts (
  script_id      INTEGER PRIMARY KEY,
  person_id      INTEGER REFERENCES persons(person_id) ON DELETE CASCADE,
  project_id     TEXT NOT NULL DEFAULT '',
  task_id        TEXT NOT NULL DEFAULT '',
  kind           TEXT NOT NULL,
  version        INTEGER NOT NULL DEFAULT 1,
  prev_script_id INTEGER REFERENCES scripts(script_id),
  body           TEXT NOT NULL,
  report_json    TEXT NOT NULL DEFAULT '',
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scripts_person ON scripts(person_id, kind, version);

CREATE TABLE IF NOT EXISTS videos (
  video_id      INTEGER PRIMARY KEY,
  person_id     INTEGER REFERENCES persons(person_id) ON DELETE SET NULL,
  project_id    TEXT NOT NULL DEFAULT '',
  script_id     INTEGER REFERENCES scripts(script_id),
  task_id       TEXT NOT NULL,
  path          TEXT NOT NULL,
  content_hash  TEXT NOT NULL DEFAULT '',
  duration_s    REAL,
  state         TEXT NOT NULL DEFAULT 'active',
  review_status TEXT NOT NULL DEFAULT 'ok',
  degraded      INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS groups (
  name       TEXT PRIMARY KEY,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS group_members (
  group_name TEXT NOT NULL REFERENCES groups(name) ON DELETE CASCADE,
  person_id  INTEGER NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
  added_at   TEXT NOT NULL,
  PRIMARY KEY (group_name, person_id)
);

CREATE TABLE IF NOT EXISTS library_links (
  person_id     INTEGER NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
  doc_id        TEXT NOT NULL,
  book_id       TEXT NOT NULL DEFAULT '',
  role          TEXT NOT NULL DEFAULT 'source',
  source_title  TEXT NOT NULL DEFAULT '',
  source_page   TEXT NOT NULL DEFAULT '',
  -- 史料库软引用行的“用于生成”开关/权重（里程碑 2 收尾 #6）。
  -- NULL=未设置，展示时按可信度推导（低可信默认不启用）。
  enabled       INTEGER,
  weight        REAL,
  PRIMARY KEY (person_id, doc_id)
);

CREATE TABLE IF NOT EXISTS review_confirmations (
  confirm_id   INTEGER PRIMARY KEY,
  video_id     INTEGER NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
  codes_json   TEXT NOT NULL,
  confirmed_at TEXT NOT NULL
);
"""

# ── M2：以“人物 → 任务”为中心的工作台底座（里程碑 1）。
#   * tasks：一次生成 = 一行；状态机/阶段/进度/参数快照/版本链/提醒，
#     支撑左侧导航“任务”、顶栏“进行中任务”抽屉与任务详情。person_id 可空
#     （文库人物未物化时仅按 person_name 归属），person_name 供列表聚合。
#   * articles：用户上传/粘贴/链接的文章（史料库文章仍走软引用），带
#     enabled/weight/reliability，供“文章”标签页勾选参与生成。
#   只建表/建索引，不改动既有表。
SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS tasks (
  task_id        TEXT PRIMARY KEY,
  person_id      INTEGER REFERENCES persons(person_id) ON DELETE SET NULL,
  person_name    TEXT NOT NULL DEFAULT '',
  title          TEXT NOT NULL DEFAULT '',
  stop_at        TEXT NOT NULL DEFAULT 'video',
  status         TEXT NOT NULL DEFAULT 'processing',
  stage          TEXT NOT NULL DEFAULT '',
  progress       INTEGER NOT NULL DEFAULT 0,
  params_json    TEXT NOT NULL DEFAULT '',
  error          TEXT NOT NULL DEFAULT '',
  failed_stage   TEXT NOT NULL DEFAULT '',
  -- 各阶段耗时秒数（里程碑 3 #1）：{"preflight":3.2,...}，供任务详情时间线。
  stage_timings  TEXT NOT NULL DEFAULT '',
  warnings_json  TEXT NOT NULL DEFAULT '',
  artifacts_json TEXT NOT NULL DEFAULT '',
  parent_task_id TEXT NOT NULL DEFAULT '',
  version        INTEGER NOT NULL DEFAULT 1,
  created_at     TEXT NOT NULL,
  started_at     TEXT,
  completed_at   TEXT,
  updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_person ON tasks(person_name, status);
CREATE INDEX IF NOT EXISTS idx_tasks_status_created ON tasks(status, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id);

CREATE TABLE IF NOT EXISTS articles (
  article_id   INTEGER PRIMARY KEY,
  public_id    TEXT NOT NULL UNIQUE,
  person_id    INTEGER REFERENCES persons(person_id) ON DELETE SET NULL,
  person_name  TEXT NOT NULL DEFAULT '',
  title        TEXT NOT NULL DEFAULT '',
  source       TEXT NOT NULL DEFAULT 'paste',
  content_ref  TEXT NOT NULL DEFAULT '',
  excerpt      TEXT NOT NULL DEFAULT '',
  enabled      INTEGER NOT NULL DEFAULT 1,
  weight       REAL NOT NULL DEFAULT 1.0,
  reliability  TEXT NOT NULL DEFAULT '',
  word_count   INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  last_used_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_articles_person ON articles(person_name, enabled);
"""

# 迁移列表：(目标 user_version, 应用函数)。只前滚，无 down。


def _apply_v3(conn: sqlite3.Connection) -> None:
    """library_links 加软引用状态列（已存在则跳过；新库由 SCHEMA_V1 直接建好）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(library_links)").fetchall()}
    if "enabled" not in cols:
        conn.execute("ALTER TABLE library_links ADD COLUMN enabled INTEGER")
    if "weight" not in cols:
        conn.execute("ALTER TABLE library_links ADD COLUMN weight REAL")


def _apply_v4(conn: sqlite3.Connection) -> None:
    """tasks 加 stage_timings 列（里程碑 3 #1 阶段耗时；新库由 SCHEMA_V2 直接建好）。

    迁移只前滚、不假设表必在：手工构造的旧库可能还没有 tasks 表（如仅 V1
    的测试夹具），此时直接跳过——后续 SCHEMA_V2 建表已自带该列。
    """
    has_tasks = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'"
    ).fetchone()
    if not has_tasks:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    if "stage_timings" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN stage_timings TEXT NOT NULL DEFAULT ''")


MIGRATIONS = [
    (1, lambda conn: conn.executescript(SCHEMA_V1)),
    (2, lambda conn: conn.executescript(SCHEMA_V2)),
    (3, _apply_v3),
    (4, _apply_v4),
]

# 当前代码支持的最高 schema 版本（= 最后一条迁移的目标版本）。
TARGET_VERSION = MIGRATIONS[-1][0] if MIGRATIONS else 0


def get_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _db_file(conn: sqlite3.Connection) -> str:
    """连接对应的数据库文件路径；内存库（:memory:）返回空串。"""
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error:
        return ""
    # database_list 行形如 (seq, "main", "/abs/path.sqlite")，内存库第三列为空。
    return (row[2] or "") if row and len(row) > 2 else ""


def _default_backups_dir(db_file: str) -> str:
    """备份目录：env `MPT_DB_BACKUPS_DIR` 优先；否则取数据库所在 storage 下的
    `backups`（mpt.sqlite 位于 storage/library/ → storage/backups）。"""
    env = os.environ.get("MPT_DB_BACKUPS_DIR", "").strip()
    if env:
        return env
    library_dir = os.path.dirname(os.path.abspath(db_file))
    storage_root = os.path.dirname(library_dir)
    return os.path.join(storage_root, "backups")


def _prune_backups(backups_dir: str, keep: int = 5) -> None:
    """只保留最近 keep 份 `mpt-*.sqlite` 快照（按修改时间降序）。"""
    try:
        names = [
            f
            for f in os.listdir(backups_dir)
            if f.startswith("mpt-") and f.endswith(".sqlite")
        ]
    except OSError:
        return
    entries = []
    for name in names:
        path = os.path.join(backups_dir, name)
        try:
            entries.append((os.path.getmtime(path), path))
        except OSError:
            continue
    entries.sort(key=lambda e: e[0], reverse=True)
    for _, path in entries[keep:]:
        try:
            os.remove(path)
        except OSError:
            pass


def backup_before_migration(
    conn: sqlite3.Connection,
    *,
    backups_dir: str | None = None,
    keep: int = 5,
) -> str | None:
    """迁移前用 `VACUUM INTO` 生成一致性快照，保留最近 keep 份。

    数据安全纪律：mpt.sqlite 已是真实用户数据，结构变更前必须先落一份可回滚
    快照。`VACUUM INTO` 写出一个独立的、事务一致的副本，不锁写主库。内存库无
    需/无法备份，直接跳过。备份是尽力而为：任何失败都不得阻断迁移（迁移本身
    只前滚、只加表/加列，不删数据），只记日志。
    """
    db_file = _db_file(conn)
    if not db_file:
        return None  # 内存库：测试/临时连接，无需备份
    target_dir = backups_dir or _default_backups_dir(db_file)
    try:
        os.makedirs(target_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%SZ", time.gmtime())
        base = os.path.splitext(os.path.basename(db_file))[0] or "mpt"
        dest = os.path.join(target_dir, f"{base}-{stamp}.sqlite")
        # VACUUM INTO 要求目标文件不存在；同一秒内重入时追加计数。
        suffix = 0
        while os.path.exists(dest):
            suffix += 1
            dest = os.path.join(target_dir, f"{base}-{stamp}-{suffix}.sqlite")
        # 路径为本模块生成、受控字符集（目录名 + 时间戳），以参数绑定写入，避免拼接转义。
        conn.execute("VACUUM INTO ?", (dest,))
        _prune_backups(target_dir, keep=keep)
        return dest
    except sqlite3.Error as exc:
        _log_migration_warning(f"pre-migration backup failed: {type(exc).__name__}: {exc}")
        return None
    except OSError as exc:
        _log_migration_warning(f"pre-migration backup io error: {type(exc).__name__}: {exc}")
        return None


def _log_migration_warning(message: str) -> None:
    # db_migrations 不引入 loguru/其余 services（保持数据底座的零依赖），
    # 备份失败降级为 stderr 警告，绝不抛断迁移。
    import sys

    print(f"[db_migrations] WARNING: {message}", file=sys.stderr)


def ensure_migrated(
    conn: sqlite3.Connection,
    *,
    backups_dir: str | None = None,
    backup: bool = True,
) -> int:
    """把库前滚到 TARGET_VERSION；返回最终版本。幂等、只加表/加列、不删数据。

    存在待应用迁移时，先做一次 `VACUUM INTO` 备份（`backup=True`，默认）再应用。
    """
    current = get_version(conn)
    pending = [(target, fn) for target, fn in MIGRATIONS if current < target]
    if pending and backup:
        # 迁移前快照；失败不阻断（迁移为纯前滚加法）。
        backup_before_migration(conn, backups_dir=backups_dir)
    for target, apply_fn in pending:
        apply_fn(conn)
        # PRAGMA user_version 不支持占位符，target 来自受控整型列表，安全内联。
        conn.execute(f"PRAGMA user_version={int(target)}")
        current = target
    return current
