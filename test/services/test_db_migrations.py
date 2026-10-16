"""统一数据底座迁移框架的合成夹具测试（阶段 2 步骤 1，§C10）。

全部使用临时目录 + 合成数据，不触网、不依赖 ffmpeg，**不含任何真实语料正文**，
人物名仅为占位（"张三"/"李四"）。锁定 db_migrations 的契约：
* 不透明 id 形态（Crockford base32 ULID + 类型前缀）。
* 连接工厂强制 PRAGMA foreign_keys=ON / WAL / isolation_level=None。
* ensure_migrated 建表、写 user_version、幂等只前滚。
* 外键级联在连接上真实生效（§A3：断言 CASCADE）。
"""

from __future__ import annotations

import sqlite3
import unittest

from app.services import db_migrations


class TestPublicId(unittest.TestCase):
    def test_ulid_is_crockford_26(self):
        u = db_migrations.new_ulid()
        self.assertEqual(len(u), 26)
        self.assertTrue(all(c in db_migrations._CROCKFORD for c in u), u)

    def test_ulid_time_prefix_monotonic(self):
        # 前 10 字符为毫秒时间戳，同一进程内先后生成的应单调不减。
        a = db_migrations.new_ulid()
        b = db_migrations.new_ulid()
        self.assertLessEqual(a[:10], b[:10])

    def test_public_id_prefix(self):
        pid = db_migrations.new_public_id("per")
        self.assertTrue(pid.startswith("per_"))
        self.assertEqual(len(pid), len("per_") + 26)


class TestConnect(unittest.TestCase):
    def setUp(self):
        self.conn = db_migrations.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_pragmas_enforced(self):
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertIsNone(self.conn.isolation_level)

    def test_wal_on_file_db(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.sqlite")
            c = db_migrations.connect(path)
            try:
                mode = c.execute("PRAGMA journal_mode").fetchone()[0]
                self.assertEqual(str(mode).lower(), "wal")
            finally:
                c.close()


class TestEnsureMigrated(unittest.TestCase):
    def setUp(self):
        self.conn = db_migrations.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    def _tables(self):
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return {r["name"] for r in rows}

    def test_creates_all_tables_and_version(self):
        v = db_migrations.ensure_migrated(self.conn)
        self.assertEqual(v, db_migrations.TARGET_VERSION)
        self.assertEqual(
            self.conn.execute("PRAGMA user_version").fetchone()[0],
            db_migrations.TARGET_VERSION,
        )
        for t in (
            "persons", "person_aliases", "facts", "assets", "person_assets",
            "scripts", "videos", "groups", "group_members", "library_links",
            "review_confirmations",
        ):
            self.assertIn(t, self._tables())

    def test_idempotent(self):
        db_migrations.ensure_migrated(self.conn)
        again = db_migrations.ensure_migrated(self.conn)
        self.assertEqual(again, db_migrations.TARGET_VERSION)


class TestBackupAndUpgrade(unittest.TestCase):
    """数据安全纪律：迁移前 VACUUM INTO 备份；旧版本库升级到新版本数据不丢。

    mpt.sqlite 现为真实用户数据，禁止删库重建——本类锁定两条契约：
    * 存在待应用迁移时会先落一份可回滚快照，且只保留最近 5 份。
    * 从只到 V1 的旧库前滚到 V2，既有 persons 行原样保留，新表被建出。
    """

    def setUp(self):
        import os
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.db_file = os.path.join(self._tmp.name, "mpt.sqlite")
        self.backups_dir = os.path.join(self._tmp.name, "backups")

    def tearDown(self):
        self._tmp.cleanup()

    def _make_v1_db_with_data(self):
        # 手动只建 V1 表并写 user_version=1，模拟一个已含真实数据的旧库。
        conn = db_migrations.connect(self.db_file)
        conn.executescript(db_migrations.SCHEMA_V1)
        conn.execute("PRAGMA user_version=1")
        ts = "2026-01-01T00:00:00Z"
        conn.execute(
            "INSERT INTO persons(public_id,display_name,origin,state,created_at,updated_at)"
            " VALUES('per_keep','张三','library','active',?,?)",
            (ts, ts),
        )
        conn.execute(
            "INSERT INTO facts(person_id,ordinal,statement) VALUES(1,0,?)",
            ("占位事实",),
        )
        return conn

    def test_upgrade_preserves_data_and_creates_v2_tables(self):
        conn = self._make_v1_db_with_data()
        try:
            v = db_migrations.ensure_migrated(conn, backups_dir=self.backups_dir)
            self.assertEqual(v, db_migrations.TARGET_VERSION)
            # 旧数据不丢。
            row = conn.execute(
                "SELECT display_name, origin FROM persons WHERE public_id='per_keep'"
            ).fetchone()
            self.assertIsNotNone(row, "升级后原有 person 应仍存在")
            self.assertEqual(row["display_name"], "张三")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0], 1)
            # 新表被建出。
            tables = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertIn("tasks", tables)
            self.assertIn("articles", tables)
        finally:
            conn.close()

    def test_backup_snapshot_created_before_migration(self):
        conn = self._make_v1_db_with_data()
        try:
            db_migrations.ensure_migrated(conn, backups_dir=self.backups_dir)
        finally:
            conn.close()
        import os

        snaps = [
            f
            for f in os.listdir(self.backups_dir)
            if f.startswith("mpt-") and f.endswith(".sqlite")
        ]
        self.assertEqual(len(snaps), 1, snaps)
        # 快照本身应是一张可打开的 V1 库（迁移前的状态），含原 person。
        snap_conn = sqlite3.connect(os.path.join(self.backups_dir, snaps[0]))
        snap_conn.row_factory = sqlite3.Row
        try:
            self.assertEqual(
                snap_conn.execute("PRAGMA user_version").fetchone()[0], 1
            )
            self.assertEqual(
                snap_conn.execute(
                    "SELECT COUNT(*) FROM persons WHERE public_id='per_keep'"
                ).fetchone()[0],
                1,
            )
        finally:
            snap_conn.close()

    def test_no_backup_when_no_pending_migration(self):
        # 已是最新版本的库再次 ensure_migrated 不应产生新快照。
        conn = self._make_v1_db_with_data()
        db_migrations.ensure_migrated(conn, backups_dir=self.backups_dir)
        conn.close()
        import os

        before = set(os.listdir(self.backups_dir))
        conn = db_migrations.connect(self.db_file)
        try:
            db_migrations.ensure_migrated(conn, backups_dir=self.backups_dir)
        finally:
            conn.close()
        after = set(os.listdir(self.backups_dir))
        self.assertEqual(before, after)

    def test_prune_keeps_recent_five(self):
        import os

        os.makedirs(self.backups_dir, exist_ok=True)
        import time as _t

        for i in range(8):
            p = os.path.join(self.backups_dir, f"mpt-2026010{i}-000000Z.sqlite")
            with open(p, "w") as fh:
                fh.write("x")
            _t.sleep(0.01)
            os.utime(p, None)
        db_migrations._prune_backups(self.backups_dir, keep=5)
        remaining = [
            f for f in os.listdir(self.backups_dir) if f.startswith("mpt-")
        ]
        self.assertEqual(len(remaining), 5)

    def test_memory_db_skips_backup(self):
        # :memory: 无需也无法备份，ensure_migrated 应照常前滚。
        conn = db_migrations.connect(":memory:")
        try:
            v = db_migrations.ensure_migrated(conn)
            self.assertEqual(v, db_migrations.TARGET_VERSION)
        finally:
            conn.close()


class TestForeignKeyCascade(unittest.TestCase):
    """§A3：连接工厂开启外键后，ON DELETE CASCADE 必须真实生效。"""

    def setUp(self):
        self.conn = db_migrations.connect(":memory:")
        db_migrations.ensure_migrated(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_delete_person_cascades_children(self):
        ts = "2026-01-01T00:00:00Z"
        cur = self.conn.execute(
            "INSERT INTO persons(public_id,display_name,origin,state,created_at,updated_at)"
            " VALUES('per_x','张三','library','active',?,?)",
            (ts, ts),
        )
        pid = cur.lastrowid
        self.conn.execute(
            "INSERT INTO person_aliases(person_id,alias,source) VALUES(?,?,?)",
            (pid, "张三", "name"),
        )
        self.conn.execute(
            "INSERT INTO facts(person_id,ordinal,statement) VALUES(?,?,?)",
            (pid, 0, "占位事实"),
        )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM person_aliases").fetchone()[0], 1)
        self.conn.execute("DELETE FROM persons WHERE person_id=?", (pid,))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM person_aliases").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0], 0)

    def test_foreign_key_violation_raises(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO facts(person_id,ordinal,statement) VALUES(?,?,?)",
                (999999, 0, "orphan"),
            )


class TestMigrationV4(unittest.TestCase):
    """迁移 4（里程碑 3 #1）：tasks 加 stage_timings 列，旧行数据不丢。"""

    def setUp(self):
        import os
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.db_file = os.path.join(self._tmp.name, "mpt.sqlite")
        self.backups_dir = os.path.join(self._tmp.name, "backups")

    def tearDown(self):
        self._tmp.cleanup()

    def test_upgrade_adds_column_and_preserves_rows(self):
        import os

        # 手工构造 v3 旧库：tasks 表无 stage_timings 列且带真实形态的行。
        conn = db_migrations.connect(self.db_file)
        conn.execute("PRAGMA user_version=3")
        conn.execute(
            """CREATE TABLE tasks (
                 task_id TEXT PRIMARY KEY,
                 person_name TEXT NOT NULL DEFAULT '',
                 title TEXT NOT NULL DEFAULT '',
                 status TEXT NOT NULL DEFAULT 'processing',
                 created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
        )
        conn.execute(
            "INSERT INTO tasks(task_id,person_name,title,created_at,updated_at)"
            " VALUES('t_keep','张三','旧任务','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')"
        )
        try:
            v = db_migrations.ensure_migrated(conn, backups_dir=self.backups_dir)
            self.assertEqual(v, db_migrations.TARGET_VERSION)
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0],
                db_migrations.TARGET_VERSION,
            )
            cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
            self.assertIn("stage_timings", cols)
            row = conn.execute(
                "SELECT stage_timings FROM tasks WHERE task_id='t_keep'"
            ).fetchone()
            self.assertEqual(row["stage_timings"], "", "旧行默认空串")
            # 幂等：再次前滚不报错、不新增备份。
            before = len(os.listdir(self.backups_dir))
            db_migrations.ensure_migrated(conn, backups_dir=self.backups_dir)
            self.assertEqual(len(os.listdir(self.backups_dir)), before)
        finally:
            conn.close()

    def test_fresh_db_has_column_via_schema_v2(self):
        conn = db_migrations.connect(":memory:")
        try:
            db_migrations.ensure_migrated(conn)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
            self.assertIn("stage_timings", cols)
        finally:
            conn.close()


class TestMigrationV5(unittest.TestCase):
    """迁移 5（里程碑 3 #2）：tasks 加归档标记列，旧行默认未归档。"""

    def setUp(self):
        import os
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.db_file = os.path.join(self._tmp.name, "mpt.sqlite")
        self.backups_dir = os.path.join(self._tmp.name, "backups")

    def tearDown(self):
        self._tmp.cleanup()

    def test_upgrade_adds_archived_columns(self):
        # 手工构造 v4 旧库：tasks 表无 archived/archived_at 列且带真实行。
        conn = db_migrations.connect(self.db_file)
        conn.execute("PRAGMA user_version=4")
        conn.execute(
            """CREATE TABLE tasks (
                 task_id TEXT PRIMARY KEY,
                 status TEXT NOT NULL DEFAULT 'processing',
                 stage_timings TEXT NOT NULL DEFAULT '',
                 created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
        )
        conn.execute(
            "INSERT INTO tasks(task_id,status,created_at,updated_at)"
            " VALUES('t_keep','completed','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')"
        )
        try:
            v = db_migrations.ensure_migrated(conn, backups_dir=self.backups_dir)
            self.assertEqual(v, db_migrations.TARGET_VERSION)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
            self.assertIn("archived", cols)
            self.assertIn("archived_at", cols)
            row = conn.execute(
                "SELECT archived, archived_at FROM tasks WHERE task_id='t_keep'"
            ).fetchone()
            self.assertEqual(row["archived"], 0, "旧行默认未归档")
            self.assertIsNone(row["archived_at"])
            # 幂等：再次前滚不新增备份。
            import os

            before = len(os.listdir(self.backups_dir))
            db_migrations.ensure_migrated(conn, backups_dir=self.backups_dir)
            self.assertEqual(len(os.listdir(self.backups_dir)), before)
        finally:
            conn.close()

    def test_fresh_db_has_archived_columns(self):
        conn = db_migrations.connect(":memory:")
        try:
            db_migrations.ensure_migrated(conn)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
            self.assertIn("archived", cols)
            self.assertIn("archived_at", cols)
        finally:
            conn.close()

    def test_v5_skips_when_no_tasks_table(self):
        # 防御：仅 V1 的旧库无 tasks 表，v5 迁移必须跳过而非报错。
        conn = db_migrations.connect(":memory:")
        try:
            conn.executescript(db_migrations.SCHEMA_V1)
            conn.execute("PRAGMA user_version=1")
            db_migrations._apply_v5(conn)  # 不应抛
        finally:
            conn.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
