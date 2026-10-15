"""tasks 表 + 人物聚合统计 + projects.json 迁移的测试（里程碑 1，§七）。

临时目录合成夹具，不触碰真实语料 / config.toml；连接经 person_repo.connect 建表并
前滚 schema（含本次新增的 tasks / articles）。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from app.services import person_repo, task_repo


class _Case(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._td.name, "mpt.sqlite")
        self.conn = person_repo.connect(self.db)

    def tearDown(self):
        self.conn.close()
        self._td.cleanup()


class TestSchema(_Case):
    def test_tasks_and_articles_tables_exist(self):
        names = {
            r["name"]
            for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("tasks", names)
        self.assertIn("articles", names)
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 2)


class TestRecordUpdate(_Case):
    def test_record_is_idempotent_and_update_maps_state(self):
        task_repo.record_task(
            "t1", person_name="曹操", title="曹操传", params={"a": 1}, conn=self.conn
        )
        # 重复登记不覆盖已存在行。
        task_repo.record_task("t1", person_name="X", conn=self.conn)
        row = task_repo.get_task("t1", conn=self.conn)
        self.assertEqual(row["person_name"], "曹操")
        self.assertEqual(row["status"], "processing")

        # 完成态：state=1 → completed，并落 completed_at。
        task_repo.update_from_state("t1", {"state": 1, "progress": 100, "videos": ["v.mp4"]}, conn=self.conn)
        row = task_repo.get_task("t1", conn=self.conn)
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["progress"], 100)
        self.assertIsNotNone(row["completed_at"])

        # 失败态：state=-1 → failed + failed_stage/error 保留。
        task_repo.record_task("t2", person_name="曹操", conn=self.conn)
        task_repo.update_from_state(
            "t2", {"state": -1, "failed_stage": "audio", "error": "boom"}, conn=self.conn
        )
        row = task_repo.get_task("t2", conn=self.conn)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["failed_stage"], "audio")
        self.assertEqual(row["error"], "boom")

    def test_list_tasks_puts_active_first(self):
        task_repo.record_task("done", person_name="A", conn=self.conn)
        task_repo.update_from_state("done", {"state": 1}, conn=self.conn)
        task_repo.record_task("run", person_name="A", conn=self.conn)
        rows = task_repo.list_tasks(conn=self.conn)
        self.assertEqual(rows[0]["task_id"], "run")  # 进行中置顶

    def test_mark_cancelled_only_hits_active(self):
        task_repo.record_task("run", person_name="曹操", conn=self.conn)
        self.assertTrue(task_repo.mark_cancelled("run", conn=self.conn))
        row = task_repo.get_task("run", conn=self.conn)
        self.assertEqual(row["status"], "cancelled")
        self.assertIsNotNone(row["completed_at"])
        # 已取消/非活动行再标取消不命中（幂等）。
        self.assertFalse(task_repo.mark_cancelled("run", conn=self.conn))
        # 草稿不是活动态，不被“取消”误伤。
        task_repo.record_task("d", person_name="曹操", conn=self.conn)
        task_repo.update_from_state("d", {"state": 1}, conn=self.conn)  # completed
        self.assertFalse(task_repo.mark_cancelled("d", conn=self.conn))

    def test_cancelled_terminal_not_overwritten(self):
        # 先修2：取消后即使流水线继续返回完成/失败，tasks 表仍固定为已取消。
        task_repo.record_task("c1", person_name="曹操", conn=self.conn)
        self.assertTrue(task_repo.mark_cancelled("c1", conn=self.conn))
        # 抽屉懒同步把实时 COMPLETE(state=1) 回写——必须被 status!='cancelled' 守卫拦住。
        task_repo.update_from_state(
            "c1", {"state": 1, "progress": 100, "videos": ["v.mp4"]}, conn=self.conn
        )
        row = task_repo.get_task("c1", conn=self.conn)
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["progress"], 0)  # 未被完成态覆盖
        # 失败态同样不得覆盖已取消终态。
        task_repo.update_from_state("c1", {"state": -1, "error": "boom"}, conn=self.conn)
        row = task_repo.get_task("c1", conn=self.conn)
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["error"], "")


class TestCountsByPerson(_Case):
    def test_aggregate_single_pass(self):
        for name, states in (("曹操", [1, 1, 4]), ("爱因斯坦", [1])):
            for i, st in enumerate(states):
                tid = f"{name}-{i}"
                task_repo.record_task(tid, person_name=name, conn=self.conn)
                task_repo.update_from_state(tid, {"state": st}, conn=self.conn)
        # 曹操另有 2 篇文章，其中 1 篇停用。
        for j, enabled in ((0, 1), (1, 0)):
            self.conn.execute(
                "INSERT INTO articles(public_id,person_name,title,enabled,created_at,"
                "updated_at) VALUES(?,?,?,?,datetime('now'),datetime('now'))",
                (f"art-{j}", "曹操", f"文{ j}", enabled),
            )
        stats = task_repo.counts_by_person(conn=self.conn)
        self.assertEqual(stats["曹操"]["video_count"], 2)
        self.assertEqual(stats["曹操"]["active_task_count"], 1)
        # 待确认角标（§二）：awaiting_confirmation 单独计数，也属于活动集合。
        task_repo.record_task("aw-1", person_name="曹操", conn=self.conn)
        self.conn.execute(
            "UPDATE tasks SET status='awaiting_confirmation' WHERE task_id='aw-1'"
        )
        stats = task_repo.counts_by_person(conn=self.conn)
        self.assertEqual(stats["曹操"]["awaiting_count"], 1)
        self.assertEqual(stats["曹操"]["active_task_count"], 2)
        self.assertEqual(stats["曹操"]["article_count"], 2)
        self.assertEqual(stats["曹操"]["article_enabled_count"], 1)
        self.assertEqual(stats["爱因斯坦"]["video_count"], 1)
        self.assertEqual(stats["爱因斯坦"]["article_count"], 0)


class TestMigrateProjects(_Case):
    _PROJECTS = [
        {"id": "p_1", "task_id": "t_1", "title": "已完成", "person": "曹操",
         "status": "done", "created_at": 1700000000.0, "updated_at": 1700000100.0, "draft": {}},
        {"id": "p_2", "task_id": "", "title": "草稿", "mode": "biography",
         "status": "draft", "created_at": 1700000000.0, "updated_at": 1700000000.0,
         "draft": {"video_subject": "诸葛亮"}},
        {"id": "p_3", "task_id": "t_3", "title": "失败", "person": "爱因斯坦",
         "status": "failed", "created_at": 1700000000.0, "updated_at": 1700000000.0, "draft": {}},
    ]

    def test_import_and_idempotent(self):
        with patch("app.services.project_store.list_projects", return_value=self._PROJECTS):
            added = task_repo.migrate_projects(conn=self.conn)
            self.assertEqual(added, 3)
            again = task_repo.migrate_projects(conn=self.conn)
            self.assertEqual(again, 0)  # 幂等
        done = task_repo.get_task("t_1", conn=self.conn)
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["person_name"], "曹操")
        draft = task_repo.get_task("draft_p_2", conn=self.conn)
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(draft["person_name"], "诸葛亮")  # 草稿回退到 draft.video_subject

    def test_draft_is_not_active(self):
        # 顶栏“进行中”与人物卡片高亮只统计活动任务，历史草稿不得混入（§二/§五）。
        self.assertNotIn("draft", task_repo.ACTIVE_STATUSES)
        self.assertNotIn("pending", task_repo.ACTIVE_STATUSES)
        with patch("app.services.project_store.list_projects", return_value=self._PROJECTS):
            task_repo.migrate_projects(conn=self.conn)
        stats = task_repo.counts_by_person(conn=self.conn)
        self.assertEqual(stats.get("诸葛亮", {}).get("active_task_count", 0), 0)
        self.assertEqual(stats["诸葛亮"]["video_count"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
