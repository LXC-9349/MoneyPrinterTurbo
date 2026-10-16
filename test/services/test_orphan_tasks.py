"""孤儿任务自动建档与历史补建（里程碑 2 收尾 #7）。

env 隔离四件套（主库/个人库/文库缺失/文章目录）验证：
- record_task / save_draft：提交时人物不存在自动物化 mpt persons 并回填
  tasks.person_id；空人名保持 NULL；重复提交幂等复用同一人物；
- person_store.ensure_person_for_task：文库/person_meta 已有档案一律不动，
  两边都没有才按自建建档（进入 list_persons 卡片可见）；
- task_repo.backfill_orphan_persons：历史孤儿任务补建 + 回填，二次执行返回 0；
- POST /drafts 端点级接线：保存草稿即为海明威类人物建档。
"""

from __future__ import annotations

import gc
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.controllers.v1 import workbench
from app.models.schema import DraftSaveRequest
from app.services import person_repo, person_store, task_repo


def _req():
    return SimpleNamespace(headers={"x-task-id": "req-1"})


class _OrphanCase(unittest.TestCase):
    """公共 harness：mpt/个人库指向临时文件，文库缺失（_in_library 恒 False）。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ,
            {
                "MPT_MAIN_DB": os.path.join(self._td.name, "mpt.sqlite"),
                "MPT_PERSONAL_DB": os.path.join(self._td.name, "personal.sqlite"),
                "MPT_ARTICLES_DIR": os.path.join(self._td.name, "articles"),
                "MPT_LIBRARY_DB": os.path.join(self._td.name, "missing.sqlite"),
            },
        )
        self._env.start()
        # 预热连接，触发 schema 迁移建表。
        c = person_repo.connect()
        c.close()

    def tearDown(self):
        self._env.stop()
        # Windows：person_store 不显式关连接，随 GC 释放后才能删临时目录。
        gc.collect()
        self._td.cleanup()

    def _count_active_persons(self, name: str) -> int:
        conn = person_repo.connect()
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM persons WHERE display_name=? AND state='active'",
                (name,),
            ).fetchone()[0]
        finally:
            conn.close()

    def _insert_raw_task(self, task_id: str, person_name: str) -> None:
        """模拟历史孤儿行：不经 record_task，直接落一条 person_id 为 NULL 的任务。"""
        conn = person_repo.connect()
        try:
            conn.execute(
                """INSERT INTO tasks(task_id,person_name,status,created_at,updated_at)
                   VALUES(?,?,'completed',?,?)""",
                (task_id, person_name, "2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z"),
            )
            conn.commit()
        finally:
            conn.close()


class TestRecordTaskPerson(_OrphanCase):
    def test_record_task_materializes_person_and_links_id(self):
        task_repo.record_task("t-hemi", person_name="海明威", title="海明威的一生")
        row = task_repo.get_task("t-hemi")
        self.assertIsNotNone(row["person_id"], "提交时人物不存在应自动物化并回填 person_id")
        self.assertEqual(self._count_active_persons("海明威"), 1)
        # 主名同步进别名表（create_or_get_person 既有语义；list_aliases 故意滤掉主名）
        conn = person_repo.connect()
        try:
            n = conn.execute(
                """SELECT COUNT(*) FROM person_aliases a
                   JOIN persons p ON p.person_id=a.person_id
                   WHERE p.display_name=? AND a.alias=?""",
                ("海明威", "海明威"),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 1)

    def test_record_task_reuses_existing_person(self):
        person_repo.create_or_get_person("海明威")
        task_repo.record_task("t1", person_name="海明威")
        task_repo.record_task("t2", person_name="海明威")
        self.assertEqual(self._count_active_persons("海明威"), 1, "重名不重复建档")
        self.assertEqual(
            task_repo.get_task("t1")["person_id"],
            task_repo.get_task("t2")["person_id"],
        )

    def test_record_task_empty_person_keeps_null_id(self):
        task_repo.record_task("t-none", person_name="  ")
        self.assertIsNone(task_repo.get_task("t-none")["person_id"])
        self.assertEqual(self._count_active_persons("  "), 0)

    def test_save_draft_creates_person_and_survives_override(self):
        task_repo.save_draft("dr-1", person_name="居里夫人", title="初稿")
        pid = task_repo.get_task("dr-1")["person_id"]
        self.assertIsNotNone(pid)
        # 反复覆盖草稿：person_id 不丢、仍是 draft
        task_repo.save_draft("dr-1", person_name="居里夫人", title="二稿", params={"a": 1})
        row = task_repo.get_task("dr-1")
        self.assertEqual(row["person_id"], pid)
        self.assertEqual(row["status"], "draft")
        self.assertEqual(row["title"], "二稿")
        self.assertEqual(self._count_active_persons("居里夫人"), 1)


class TestEnsurePersonForTask(_OrphanCase):
    def test_creates_custom_person_visible_on_cards(self):
        self.assertTrue(person_store.ensure_person_for_task("海明威"))
        meta = person_store.person_meta("海明威")
        self.assertEqual(meta["custom"], 1)
        cards = {p["person"]: p for p in person_store.list_persons()}
        self.assertIn("海明威", cards)
        self.assertEqual(cards["海明威"]["source"], "custom")
        # 再次提交任务不重复建档、返回 False
        self.assertFalse(person_store.ensure_person_for_task("海明威"))

    def test_skips_library_figure(self):
        with patch.object(person_store, "_in_library", lambda name: True):
            self.assertFalse(person_store.ensure_person_for_task("曹操"))
        self.assertIsNone(person_store.person_meta("曹操"), "文库人物不写 person_meta")

    def test_never_overwrites_existing_archive(self):
        person_store.create_custom_person("海明威", note="小说家")
        self.assertFalse(person_store.ensure_person_for_task("海明威"))
        meta = person_store.person_meta("海明威")
        self.assertEqual(meta["note"], "小说家", "已有档案一律不动")

    def test_empty_name_returns_false(self):
        self.assertFalse(person_store.ensure_person_for_task(""))
        self.assertFalse(person_store.ensure_person_for_task("   "))


class TestBackfillOrphanPersons(_OrphanCase):
    def test_backfill_creates_persons_and_fills_ids(self):
        self._insert_raw_task("old-1", "海明威")
        self._insert_raw_task("old-2", "居里夫人")
        self._insert_raw_task("old-3", "海明威")  # 同名多任务只建一次档
        n = task_repo.backfill_orphan_persons()
        self.assertEqual(n, 2)
        self.assertEqual(self._count_active_persons("海明威"), 1)
        for tid in ("old-1", "old-2", "old-3"):
            self.assertIsNotNone(task_repo.get_task(tid)["person_id"], tid)
        # 卡片可见性：文库没有的名字同步补 person_meta 自建档案
        self.assertEqual(person_store.person_meta("海明威")["custom"], 1)

    def test_backfill_idempotent(self):
        self._insert_raw_task("old-1", "海明威")
        self.assertEqual(task_repo.backfill_orphan_persons(), 1)
        self.assertEqual(task_repo.backfill_orphan_persons(), 0, "无剩余孤儿时返回 0")

    def test_backfill_skips_empty_and_existing_persons(self):
        task_repo.record_task("t-new", person_name="曹操")  # 已建档非孤儿
        self._insert_raw_task("old-blank", "")
        self._insert_raw_task("old-pace", "李世民")
        self.assertEqual(task_repo.backfill_orphan_persons(), 1, "只处理真孤儿")
        self.assertIsNone(task_repo.get_task("old-blank")["person_id"])
        self.assertIsNotNone(task_repo.get_task("old-pace")["person_id"])


class TestDraftEndpointWiring(_OrphanCase):
    def test_save_draft_endpoint_builds_person_archive(self):
        with patch.object(workbench, "_ensure_projects_migrated", lambda: None):
            resp = workbench.save_draft_endpoint(
                _req(), DraftSaveRequest(person="海明威", title="草稿", draft_id="dr_ep")
            )["data"]
        self.assertEqual(resp["status"], "draft")
        self.assertIsNotNone(resp["person_id"])
        self.assertEqual(person_store.person_meta("海明威")["custom"], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
