"""人物聚合根 repo 的合成夹具测试（阶段 2 步骤 1，§C10）。

临时目录 + 合成数据，不含真实语料正文；人物名仅占位。覆盖：
* 惰性物化 create_or_get_person 的幂等复用（§B5）。
* resolve_by_name 经别名命中，并沿 merged_into 重定向（§B7）。
* merge_person 迁移子表引用、被并方 archived + merged_into。
* upsert_asset 按 (content_hash, kind) 去重（§A2）。
* 软删除只改 state；硬删除事务内删行→提交后删文件（§A3）。
* reconcile 标 file_missing（§B8）。
"""

from __future__ import annotations

import os
import tempfile
import unittest

from app.services import person_repo


class _RepoCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = self._td.name
        self.db = os.path.join(self.root, "mpt.sqlite")
        self.conn = person_repo.connect(self.db)

    def tearDown(self):
        self.conn.close()
        self._td.cleanup()


class TestCreateResolve(_RepoCase):
    def test_create_then_reuse(self):
        a = person_repo.create_or_get_person("张三", dynasty="宋", conn=self.conn)
        self.assertTrue(a["public_id"].startswith("per_"))
        self.assertEqual(a["display_name"], "张三")
        b = person_repo.create_or_get_person("张三", conn=self.conn)
        self.assertEqual(a["person_id"], b["person_id"])  # 惰性复用，不新建
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0], 1
        )

    def test_empty_name_rejected(self):
        with self.assertRaises(ValueError):
            person_repo.create_or_get_person("   ", conn=self.conn)

    def test_resolve_by_alias(self):
        p = person_repo.create_or_get_person("李四", conn=self.conn)
        person_repo.add_alias(p["public_id"], "李四先生", source="manual", conn=self.conn)
        hit = person_repo.resolve_by_name("李四先生", conn=self.conn)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["public_id"], p["public_id"])


class TestMerge(_RepoCase):
    def test_merge_redirects_and_moves_children(self):
        src = person_repo.create_or_get_person("王五", conn=self.conn)
        dst = person_repo.create_or_get_person("赵六", conn=self.conn)
        person_repo.add_fact(src["public_id"], "事实A", conn=self.conn)
        person_repo.merge_person(src["public_id"], dst["public_id"], conn=self.conn)

        src_row = person_repo.get_person(src["public_id"], conn=self.conn)
        self.assertEqual(src_row["state"], "archived")
        self.assertEqual(src_row["merged_into"], dst["public_id"])

        # 事实迁移到存活方。
        cnt = self.conn.execute(
            "SELECT COUNT(*) FROM facts WHERE person_id=?", (dst["person_id"],)
        ).fetchone()[0]
        self.assertEqual(cnt, 1)

        # 通过被并方的旧主名解析 → 沿 merged_into 落到存活方。
        resolved = person_repo.resolve_by_name("王五", conn=self.conn)
        self.assertEqual(resolved["public_id"], dst["public_id"])


class TestAsset(_RepoCase):
    def test_upsert_asset_dedup(self):
        aid1 = person_repo.upsert_asset(
            self.conn, kind="image", path="a/1.jpg", content_hash="h1"
        )
        aid2 = person_repo.upsert_asset(
            self.conn, kind="image", path="a/copy.jpg", content_hash="h1"
        )
        self.assertEqual(aid1, aid2)  # 同哈希同类型 → 复用
        aid3 = person_repo.upsert_asset(
            self.conn, kind="audio", path="a/1.mp3", content_hash="h1"
        )
        self.assertNotEqual(aid1, aid3)  # 类型不同 → 新实体
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0], 2
        )

    def test_link_person_asset(self):
        p = person_repo.create_or_get_person("孙七", conn=self.conn)
        aid = person_repo.upsert_asset(
            self.conn, kind="image", path="a/2.jpg", content_hash="h2"
        )
        person_repo.link_person_asset(self.conn, p["public_id"], aid, state="selected", is_cover=True)
        row = self.conn.execute(
            "SELECT * FROM person_assets WHERE person_id=? AND asset_id=?",
            (p["person_id"], aid),
        ).fetchone()
        self.assertEqual(row["state"], "selected")
        self.assertEqual(row["is_cover"], 1)


class TestDelete(_RepoCase):
    def test_soft_delete_only_state(self):
        p = person_repo.create_or_get_person("周八", conn=self.conn)
        person_repo.soft_delete_person(p["public_id"], conn=self.conn)
        row = person_repo.get_person(p["public_id"], conn=self.conn)
        self.assertEqual(row["state"], "archived")
        self.assertIsNotNone(row["archived_at"])

    def test_hard_delete_removes_row_and_file(self):
        p = person_repo.create_or_get_person("吴九", conn=self.conn)
        # 建一个仅被该人物引用的素材文件。
        rel = "assets/only.jpg"
        full = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write("x")
        aid = person_repo.upsert_asset(
            self.conn, kind="image", path=rel, content_hash="hx"
        )
        person_repo.link_person_asset(self.conn, p["public_id"], aid)

        removed = person_repo.hard_delete_person(
            p["public_id"], storage_root=self.root, conn=self.conn
        )
        self.assertIn(rel, removed)
        self.assertFalse(os.path.exists(full))  # 提交后文件被删
        self.assertIsNone(person_repo.get_person(p["public_id"], conn=self.conn))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0], 0
        )


class TestReconcile(_RepoCase):
    def test_marks_missing_files(self):
        p = person_repo.create_or_get_person("郑十", conn=self.conn)
        aid = person_repo.upsert_asset(
            self.conn, kind="image", path="assets/ghost.jpg", content_hash="hg"
        )
        person_repo.link_person_asset(self.conn, p["public_id"], aid)

        report = person_repo.reconcile(self.conn, storage_root=self.root, apply=False)
        self.assertIn("assets/ghost.jpg", [m.replace("\\", "/") for m in report["missing"]])
        # 预览模式不写库。
        st = self.conn.execute("SELECT state FROM assets WHERE asset_id=?", (aid,)).fetchone()[0]
        self.assertEqual(st, "active")

        report2 = person_repo.reconcile(self.conn, storage_root=self.root, apply=True)
        self.assertIn("assets/ghost.jpg", [m.replace("\\", "/") for m in report2["marked"]])
        st2 = self.conn.execute("SELECT state FROM assets WHERE asset_id=?", (aid,)).fetchone()[0]
        self.assertEqual(st2, "file_missing")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
