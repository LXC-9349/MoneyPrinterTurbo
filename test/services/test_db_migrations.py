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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
