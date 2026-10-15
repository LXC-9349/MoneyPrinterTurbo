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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
