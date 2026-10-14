"""project_store 数据层测试：真实读写临时目录下的 projects.json。

覆盖从 webui 下沉的核心行为——创建/draft 往返/列表过滤/删除含任务目录清理/
元字段更新——保证与 Streamlit 端的数据契约（字段、路径、状态机）一致。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from app.models.schema import VideoParams
from app.services import project_store


class TestProjectStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # 把存储根重定向到临时目录：storage_dir("tasks") → tmp/tasks，无参 → tmp。
        self._patcher = patch.object(
            project_store.utils,
            "storage_dir",
            side_effect=lambda sub="", create=False: (
                os.path.join(self.tmp, sub) if sub else self.tmp
            ),
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _read_raw(self):
        with open(
            os.path.join(self.tmp, "projects", "projects.json"), "r", encoding="utf-8"
        ) as f:
            return json.load(f)

    def test_create_project_biography_fixes_historical_source(self):
        entry = project_store.create_project("biography")
        self.assertEqual(entry["mode"], "biography")
        self.assertEqual(entry["status"], "draft")
        self.assertEqual(entry["step"], project_store.STEP_CONTENT)
        self.assertTrue(entry["id"].startswith("p_"))
        # 传记项目把素材源固定为历史档案，写入 draft 快照。
        self.assertEqual(entry["draft"]["video_source"], "historical_archive")
        # 落盘可被 list 读回。
        self.assertIn(entry["id"], [p["id"] for p in project_store.list_projects()])

    def test_create_project_normal_uses_default_source(self):
        entry = project_store.create_project("normal", title="我的项目")
        self.assertEqual(entry["title"], "我的项目")
        self.assertEqual(entry["mode"], "normal")
        self.assertNotEqual(entry["draft"]["video_source"], "historical_archive")

    def test_save_and_load_draft_roundtrip(self):
        entry = project_store.create_project("normal")
        pid = entry["id"]
        params = VideoParams(video_subject="春天的花海", video_language="zh-CN")
        params.biography_person = ""
        self.assertTrue(project_store.save_draft(pid, params, step="visual"))

        reloaded = project_store.load_draft(pid)
        self.assertEqual(reloaded.video_subject, "春天的花海")
        self.assertEqual(reloaded.video_language, "zh-CN")
        # step 同步更新到条目元数据。
        self.assertEqual(project_store.get_entry(pid)["step"], "visual")

    def test_load_draft_falls_back_to_default_for_unknown(self):
        params = project_store.load_draft("does-not-exist")
        self.assertEqual(params.video_subject, "")

    def test_list_projects_requires_task_or_draft(self):
        # 手工写入一条既无 task 也无 draft 的脏条目，应被过滤掉。
        pdir = os.path.join(self.tmp, "projects")
        os.makedirs(pdir, exist_ok=True)
        payload = {
            "projects": [
                {"id": "p_empty", "task_id": "", "draft": None},
                {"id": "p_draft", "task_id": "", "draft": {"video_subject": "x"}},
                {"id": "p_task", "task_id": "t-1"},
            ]
        }
        with open(os.path.join(pdir, "projects.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f)
        ids = {p["id"] for p in project_store.list_projects()}
        self.assertEqual(ids, {"p_draft", "p_task"})

    def test_delete_project_purges_bound_task_dir(self):
        entry = project_store.create_project("normal")
        pid = entry["id"]
        task_id = "task-to-delete"
        project_store.bind_task(pid, task_id)
        task_path = os.path.join(self.tmp, "tasks", task_id)
        os.makedirs(task_path, exist_ok=True)
        with open(os.path.join(task_path, "script.json"), "w") as f:
            f.write("{}")

        self.assertTrue(project_store.delete_project(pid))
        self.assertIsNone(project_store.get_entry(pid))
        self.assertFalse(os.path.isdir(task_path))

    def test_delete_project_rejects_path_escape(self):
        # 异常 task_id 含 ../ 时不得删除任务根目录之外的内容。
        entry = project_store.create_project("normal")
        pid = entry["id"]
        project_store.bind_task(pid, "../outside")
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside, exist_ok=True)

        project_store.delete_project(pid)
        # 越界目录应完好无损。
        self.assertTrue(os.path.isdir(outside))

    def test_update_entry_and_normalize_status(self):
        entry = project_store.create_project("biography")
        pid = entry["id"]
        updated = project_store.update_entry(
            pid, step="audio", status="bogus", person="钱学森", language="zh-CN"
        )
        self.assertEqual(updated["step"], "audio")
        # 非法状态归一化为 draft。
        self.assertEqual(updated["status"], "draft")
        self.assertEqual(updated["person"], "钱学森")
        self.assertEqual(project_store.project_mode(updated), "biography")

    def test_summary_projection_strips_draft(self):
        entry = project_store.create_project("normal")
        proj = project_store.summary_projection(entry)
        self.assertNotIn("draft", proj)
        self.assertTrue(proj["has_draft"])
        self.assertEqual(proj["step"], project_store.STEP_CONTENT)


if __name__ == "__main__":
    unittest.main()
