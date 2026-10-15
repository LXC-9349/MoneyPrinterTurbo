"""workbench 里程碑 2 端点：文章 CRUD、人物详情聚合、草稿保存。

直连端点函数（stub Request + env 隔离临时库/文章目录），验证参数接线与低可信
默认不启用、library 缺 doc_id 报 400、详情聚合计数、草稿落 draft 行。
"""

from __future__ import annotations

import gc
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.controllers.v1 import workbench
from app.models.exception import HttpException
from app.models.schema import ArticleCreateRequest, DraftSaveRequest
from app.services import article_repo, person_repo, task_repo


def _req():
    return SimpleNamespace(headers={"x-task-id": "req-1"})


class _EndpointCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ,
            {
                "MPT_MAIN_DB": os.path.join(self._td.name, "mpt.sqlite"),
                "MPT_ARTICLES_DIR": os.path.join(self._td.name, "articles"),
                # 隔离真实史料库：详情聚合的软引用行在本套测试里恒为空，
                # 软引用行为由 test_person_library_soft.py 用合成文库单独验。
                "MPT_LIBRARY_DB": os.path.join(self._td.name, "missing.sqlite"),
                # 草稿保存会自动建档（里程碑 2 收尾 #7）触 person_store，
                # 必须隔离个人库，避免污染真实 personal.sqlite。
                "MPT_PERSONAL_DB": os.path.join(self._td.name, "personal.sqlite"),
            },
        )
        self._env.start()
        # 预热连接，触发 schema 迁移建表。
        c = person_repo.connect()
        c.close()
        # projects.json 惰性迁移与端点逻辑无关，置空避免触真实 project_store。
        self._patch = patch.object(workbench, "_ensure_projects_migrated", lambda: None)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._env.stop()
        # Windows：person_store 的连接随 GC 才释放，先回收再删临时目录。
        gc.collect()
        self._td.cleanup()


class TestArticleEndpoints(_EndpointCase):
    def test_add_paste_article_defaults_enabled(self):
        resp = workbench.add_person_article(
            _req(),
            "曹操",
            ArticleCreateRequest(source="paste", content="曹操孟德，乱世之枭雄。", title="武帝"),
        )
        data = resp["data"]
        self.assertEqual(data["source"], "paste")
        self.assertEqual(data["enabled"], 1)
        listed = workbench.list_person_articles(_req(), "曹操")["data"]["articles"]
        self.assertEqual(len(listed), 1)

    def test_add_low_reliability_defaults_disabled(self):
        resp = workbench.add_person_article(
            _req(),
            "曹操",
            ArticleCreateRequest(source="upload", content="存疑正文", reliability="low"),
        )
        self.assertEqual(resp["data"]["enabled"], 0)

    def test_library_article_requires_doc_id(self):
        with self.assertRaises(HttpException) as raised:
            workbench.add_person_article(
                _req(), "曹操", ArticleCreateRequest(source="library", title="史")
            )
        self.assertEqual(raised.exception.status_code, 400)

    def test_patch_and_delete(self):
        from app.models.schema import ArticlePatchRequest

        art = workbench.add_person_article(
            _req(), "曹操", ArticleCreateRequest(source="paste", content="正文内容")
        )["data"]
        patched = workbench.patch_article(
            _req(), art["article_id"], ArticlePatchRequest(enabled=False, weight=2.0)
        )["data"]
        self.assertEqual(patched["enabled"], 0)
        self.assertAlmostEqual(patched["weight"], 2.0)
        deleted = workbench.delete_article(_req(), art["article_id"])["data"]
        self.assertTrue(deleted["deleted"])


class TestPersonDetail(_EndpointCase):
    def test_detail_aggregates_counts(self):
        # 一篇启用文章 + 一个已完成任务（带成片 artifacts + 中间合成件）。
        article_repo.add_article("曹操", "文", source="paste", content="内容")
        task_repo.record_task("done1", person_name="曹操", conn=None)
        task_repo.update_from_state(
            "done1",
            {
                "state": 1,
                "progress": 100,
                "videos": ["final-1.mp4"],
                "combined_videos": ["combined-1.mp4"],
            },
            conn=None,
        )
        profile = {"person": "曹操", "key_facts": ["事实一", "事实二"], "docs": []}
        with patch.object(workbench.person_store, "get_person", return_value=profile):
            resp = workbench.person_detail(_req(), "曹操")["data"]
        self.assertEqual(resp["summary"]["article_count"], 1)
        self.assertEqual(resp["summary"]["article_enabled_count"], 1)
        self.assertEqual(resp["summary"]["fact_count"], 2)
        # 中间合成件不得登记也不得计入视频数：仅 final-1.mp4 计为 1。
        self.assertEqual(resp["summary"]["video_count"], 1)
        self.assertEqual([v["path"] for v in resp["videos"]], ["final-1.mp4"])
        self.assertNotIn(
            "combined-1.mp4", {v["path"] for v in resp["videos"]}
        )

    def test_detail_404_when_unknown(self):
        with patch.object(workbench.person_store, "get_person", return_value=None):
            with self.assertRaises(HttpException) as raised:
                workbench.person_detail(_req(), "无名氏")
        self.assertEqual(raised.exception.status_code, 404)


class TestDraftEndpoint(_EndpointCase):
    def test_save_draft_returns_draft_row(self):
        resp = workbench.save_draft_endpoint(
            _req(),
            DraftSaveRequest(person="曹操", title="草稿", params={"voice_name": "x"}, draft_id="dr_x"),
        )["data"]
        self.assertEqual(resp["task_id"], "dr_x")
        self.assertEqual(resp["status"], "draft")

    def test_delete_draft(self):
        workbench.save_draft_endpoint(_req(), DraftSaveRequest(person="曹操", draft_id="dr_y"))
        out = workbench.delete_draft_endpoint(_req(), "dr_y")["data"]
        self.assertTrue(out["deleted"])


class TestTaskBatchEndpoints(_EndpointCase):
    """任务列表批量操作/重试/导出端点（里程碑 3 #2）。"""

    def test_batch_cancel_marks_active(self):
        from app.models.schema import TaskBatchRequest

        task_repo.record_task("b-run", person_name="曹操", conn=None)
        out = workbench.batch_tasks(
            _req(), TaskBatchRequest(task_ids=["b-run"], action="cancel")
        )["data"]
        self.assertEqual(out["affected"], ["b-run"])
        self.assertEqual(task_repo.get_task("b-run", conn=None)["status"], "cancelled")

    def test_batch_archive_then_list_hides(self):
        from app.models.schema import TaskBatchRequest

        task_repo.record_task("b-arch", person_name="曹操", conn=None)
        task_repo.update_from_state("b-arch", {"state": 1}, conn=None)  # completed
        out = workbench.batch_tasks(
            _req(), TaskBatchRequest(task_ids=["b-arch"], action="archive")
        )["data"]
        self.assertEqual(out["count"], 1)
        listed = workbench.list_tasks(
            _req(), status="", person="", keyword="", archived=0, limit=200
        )["data"]
        self.assertNotIn("b-arch", {t["task_id"] for t in listed["tasks"]})
        # counts 不含归档行。
        self.assertNotIn("b-arch", [t["task_id"] for t in listed["tasks"]])

    def test_batch_delete_skips_active(self):
        from app.models.schema import TaskBatchRequest

        task_repo.record_task("b-done", person_name="曹操", conn=None)
        task_repo.update_from_state("b-done", {"state": 1}, conn=None)
        task_repo.record_task("b-live", person_name="曹操", conn=None)  # processing
        out = workbench.batch_tasks(
            _req(),
            TaskBatchRequest(task_ids=["b-done", "b-live"], action="delete"),
        )["data"]
        self.assertEqual(out["affected"], ["b-done"])  # 活动态不删
        self.assertIsNone(task_repo.get_task("b-done", conn=None))
        self.assertIsNotNone(task_repo.get_task("b-live", conn=None))

    def test_batch_rejects_unknown_action(self):
        from app.models.schema import TaskBatchRequest

        with self.assertRaises(HttpException) as raised:
            workbench.batch_tasks(_req(), TaskBatchRequest(task_ids=["x"], action="nuke"))
        self.assertEqual(raised.exception.status_code, 400)

    def test_retry_creates_new_version(self):
        import json as _json

        task_repo.record_task("r-orig", person_name="曹操", conn=None)
        task_repo.update_from_state("r-orig", {"state": -1, "failed_stage": "audio"}, conn=None)
        # 写入可重建的参数快照。
        self.conn = None  # 端点默认连接，直接走全局
        conn = person_repo.connect()
        try:
            conn.execute(
                "UPDATE tasks SET params_json=? WHERE task_id='r-orig'",
                (_json.dumps({"video_subject": "曹操", "biography_person": "曹操"}),),
            )
        finally:
            conn.close()

        captured = {}

        def _fake_enqueue(task_id, body, stop_at, **kw):
            captured["stop_at"] = stop_at
            captured["parent"] = kw.get("parent_task_id")
            captured["version"] = kw.get("version")
            task_repo.record_task(
                task_id,
                person_name=kw.get("person_name", ""),
                stop_at=stop_at,
                params={"video_subject": "曹操"},
                parent_task_id=kw.get("parent_task_id", ""),
                version=kw.get("version", 1),
                conn=None,
            )

        with patch("app.controllers.v1.video.enqueue_generation", _fake_enqueue):
            resp = workbench.retry_task(_req(), "r-orig")["data"]
        self.assertEqual(captured["stop_at"], "video")
        self.assertEqual(captured["parent"], "r-orig")
        self.assertEqual(captured["version"], 2)
        self.assertEqual(resp["retry_of"], "r-orig")

    def test_retry_rejects_active(self):
        task_repo.record_task("r-live", person_name="曹操", conn=None)  # processing
        with self.assertRaises(HttpException) as raised:
            workbench.retry_task(_req(), "r-live")
        self.assertEqual(raised.exception.status_code, 409)

    def test_export_404_when_no_file(self):
        task_repo.record_task("e-none", person_name="曹操", conn=None)
        task_repo.update_from_state("e-none", {"state": 1, "videos": []}, conn=None)
        with self.assertRaises(HttpException) as raised:
            workbench.export_task(_req(), "e-none")
        self.assertEqual(raised.exception.status_code, 404)

    def test_export_returns_file(self):
        root = os.path.join(self._td.name, "tasks")
        os.makedirs(os.path.join(root, "e-ok"), exist_ok=True)
        video_path = os.path.join(root, "e-ok", "final-1.mp4")
        with open(video_path, "wb") as fh:
            fh.write(b"data")
        task_repo.record_task("e-ok", person_name="曹操", conn=None)
        task_repo.update_from_state(
            "e-ok", {"state": 1, "videos": ["e-ok/final-1.mp4"]}, conn=None
        )
        with patch.object(workbench.utils, "task_dir", return_value=root):
            resp = workbench.export_task(_req(), "e-ok")
        self.assertEqual(resp.__class__.__name__, "FileResponse")


class TestTaskDetail(_EndpointCase):
    """任务详情端点（里程碑 3 #3）：时间线 / 版本链 / 参数产物解析 / 内联视频。"""

    def test_detail_builds_timeline_and_lineage(self):
        import json as _json

        task_repo.record_task("d-orig", person_name="曹操", conn=None)
        for stage, secs in (("preflight", 0.1), ("script", 12.5), ("terms", 1.2)):
            task_repo.record_stage_timing("d-orig", stage, secs, conn=None)
        # 一条重试子任务，验证版本链聚合。
        task_repo.record_task(
            "d-child", person_name="曹操", parent_task_id="d-orig", version=2, conn=None
        )
        conn = person_repo.connect()
        try:
            conn.execute(
                "UPDATE tasks SET params_json=? WHERE task_id='d-orig'",
                (_json.dumps({"video_subject": "曹操", "voice_name": "x"}),),
            )
        finally:
            conn.close()
        data = workbench.task_detail(_req(), "d-orig")["data"]
        self.assertEqual(data["params"]["video_subject"], "曹操")
        stages = [seg["stage"] for seg in data["timeline"]]
        self.assertEqual(stages, ["preflight", "script", "terms"])
        self.assertTrue(all(seg["status"] == "done" for seg in data["timeline"]))
        self.assertAlmostEqual(data["total_seconds"], 13.8, places=1)
        # 版本链含自身 + 子任务。
        self.assertEqual(
            {v["task_id"] for v in data["versions"]}, {"d-orig", "d-child"}
        )

    def test_detail_marks_running_stage(self):
        task_repo.record_task("d-live", person_name="曹操", conn=None)  # processing
        conn = person_repo.connect()
        try:
            conn.execute(
                "UPDATE tasks SET stage='script' WHERE task_id='d-live'"
            )
        finally:
            conn.close()
        data = workbench.task_detail(_req(), "d-live")["data"]
        running = [s for s in data["timeline"] if s["status"] == "running"]
        self.assertEqual([s["stage"] for s in running], ["script"])

    def test_detail_404_when_unknown(self):
        with self.assertRaises(HttpException) as raised:
            workbench.task_detail(_req(), "nope")
        self.assertEqual(raised.exception.status_code, 404)

    def test_video_stream_returns_file(self):
        root = os.path.join(self._td.name, "tasks")
        os.makedirs(os.path.join(root, "v-ok"), exist_ok=True)
        with open(os.path.join(root, "v-ok", "final-1.mp4"), "wb") as fh:
            fh.write(b"data")
        task_repo.record_task("v-ok", person_name="曹操", conn=None)
        task_repo.update_from_state(
            "v-ok", {"state": 1, "videos": ["v-ok/final-1.mp4"]}, conn=None
        )
        with patch.object(workbench.utils, "task_dir", return_value=root):
            resp = workbench.task_video_stream(_req(), "v-ok")
        self.assertEqual(resp.__class__.__name__, "FileResponse")


class TestTaskResume(_EndpointCase):
    """从失败阶段继续 + 失败原因人话化（里程碑 3 #4）。"""

    def _fail_task(self, tid, error, failed_stage):
        import json as _json

        task_repo.record_task(tid, person_name="曹操", conn=None)
        task_repo.update_from_state(
            tid, {"state": -1, "failed_stage": failed_stage, "error": error}, conn=None
        )
        conn = person_repo.connect()
        try:
            conn.execute(
                "UPDATE tasks SET params_json=? WHERE task_id=?",
                (_json.dumps({"video_subject": "曹操"}), tid),
            )
        finally:
            conn.close()

    def test_resume_rejects_active(self):
        task_repo.record_task("rs-live", person_name="曹操", conn=None)  # processing
        with self.assertRaises(HttpException) as raised:
            workbench.resume_task(_req(), "rs-live")
        self.assertEqual(raised.exception.status_code, 409)

    def test_resume_resets_and_redispatches_same_id(self):
        self._fail_task("rs-fail", "Volcengine requires an Ark API key", "materials")
        captured = {}

        def _fake_enqueue(task_id, body, stop_at, **kw):
            captured["id"] = task_id
            captured["stop_at"] = stop_at

        with patch("app.controllers.v1.video.enqueue_generation", _fake_enqueue):
            resp = workbench.resume_task(_req(), "rs-fail")["data"]
        # 同一 task_id 重新派发（不新建版本）。
        self.assertEqual(captured["id"], "rs-fail")
        self.assertTrue(resp["resumed"])
        row = task_repo.get_task("rs-fail", conn=None)
        self.assertEqual(row["status"], "processing")
        self.assertEqual(row["error"], "")
        self.assertEqual(row["failed_stage"], "")

    def test_humanize_failure_via_detail(self):
        self._fail_task("rs-api", "Pexels video generation requires a Pexels API key", "materials")
        data = workbench.task_detail(_req(), "rs-api")["data"]
        failure = data["failure"]
        self.assertIsNotNone(failure)
        self.assertIn("API Key", failure["reason"])
        self.assertEqual(failure["fix"], "settings")
        self.assertEqual(failure["stage"], "materials")

    def test_humanize_failure_none_when_completed(self):
        task_repo.record_task("rs-ok", person_name="曹操", conn=None)
        task_repo.update_from_state("rs-ok", {"state": 1}, conn=None)
        data = workbench.task_detail(_req(), "rs-ok")["data"]
        self.assertIsNone(data["failure"])


class TestQueueControl(unittest.TestCase):
    """队列暂停/恢复/并发（里程碑 3 #5）：直接验调度器基类行为。"""

    def _mgr(self, concurrency=1):
        from app.controllers.manager.memory_manager import InMemoryTaskManager

        return InMemoryTaskManager(max_concurrent_tasks=concurrency, max_queued_tasks=10)

    def test_pause_blocks_dispatch_then_resume_runs(self):
        import time

        mgr = self._mgr(1)
        mgr.pause()
        self.assertTrue(mgr.is_paused())
        called: list[int] = []
        mgr.add_task(lambda **kw: called.append(1), task_id="q1")
        # 暂停中：只入队不执行。
        self.assertEqual(mgr.queue_size(), 1)
        self.assertEqual(called, [])
        stats = mgr.queue_stats()
        self.assertTrue(stats["paused"])
        self.assertEqual(stats["queued"], 1)
        # 恢复后立即派发（子线程执行）。
        mgr.resume()
        for _ in range(50):
            if called:
                break
            time.sleep(0.02)
        self.assertEqual(called, [1])
        self.assertFalse(mgr.is_paused())

    def test_set_concurrency(self):
        mgr = self._mgr(1)
        self.assertEqual(mgr.get_concurrency(), 1)
        mgr.set_concurrency(3)
        self.assertEqual(mgr.queue_stats()["max_concurrent_tasks"], 3)
        with self.assertRaises(ValueError):
            mgr.set_concurrency(-1)


class TestInterruptedOnStartup(_EndpointCase):
    """服务重启标记中断 + 中断可续跑（里程碑 3 #5）。"""

    def _set_params(self, tid):
        import json as _json

        conn = person_repo.connect()
        try:
            conn.execute(
                "UPDATE tasks SET params_json=? WHERE task_id=?",
                (_json.dumps({"video_subject": "曹操"}), tid),
            )
        finally:
            conn.close()

    def test_mark_interrupted_converts_active_only(self):
        task_repo.record_task("live1", person_name="曹操", conn=None)  # processing
        task_repo.record_task("live2", person_name="曹操", conn=None)
        task_repo.update_from_state("live2", {"state": 2}, conn=None)  # queued
        task_repo.record_task("done1", person_name="曹操", conn=None)
        task_repo.update_from_state("done1", {"state": 1}, conn=None)  # completed
        n = task_repo.mark_interrupted_on_startup(conn=None)
        self.assertEqual(n, 2)
        self.assertEqual(task_repo.get_task("live1", conn=None)["status"], "interrupted")
        self.assertEqual(task_repo.get_task("live2", conn=None)["status"], "interrupted")
        self.assertEqual(task_repo.get_task("done1", conn=None)["status"], "completed")
        # counts 含 interrupted。
        self.assertEqual(workbench._task_counts()["interrupted"], 2)

    def test_interrupted_is_resumable_and_humanized(self):
        task_repo.record_task("int1", person_name="曹操", conn=None)
        self._set_params("int1")
        task_repo.mark_interrupted_on_startup(conn=None)
        data = workbench.task_detail(_req(), "int1")["data"]
        self.assertIsNotNone(data["failure"])
        self.assertIn("中断", data["failure"]["reason"])
        self.assertEqual(data["failure"]["fix"], "resume")
        captured = {}

        def _fake(task_id, body, stop_at, **kw):
            captured["id"] = task_id

        with patch("app.controllers.v1.video.enqueue_generation", _fake):
            resp = workbench.resume_task(_req(), "int1")["data"]
        self.assertEqual(captured["id"], "int1")
        self.assertTrue(resp["resumed"])
        self.assertEqual(task_repo.get_task("int1", conn=None)["status"], "processing")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
