"""工作台仪表盘端点的回归测试（里程碑 1，§二/§七）。

直接调用控制器函数（纯逻辑 + 依赖打桩），不建真实 DB / 语料 / HTTP，避免触碰
959MB 史料库与 config.toml。重点锁定三条不变量：
1. 聚合统计单次获取：counts_by_person 只被调用一次，杜绝逐人 N+1。
2. 候选排除：有任意任务/文章历史（含失败）的人物不进入"史料库尚未使用"候选。
3. 进行中抽屉不含草稿：/tasks/active 只返回 ACTIVE_STATUSES。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.controllers.v1 import workbench
from app.services import task_repo


class _Req:  # 端点只用 request 占位，不需要真实内容。
    pass


def _person(name, *, dynasty="", source="library", doc_count=0, pinned=False):
    return {
        "person": name,
        "dynasty": dynasty,
        "category": "",
        "doc_count": doc_count,
        "source": source,
        "pinned": pinned,
        "hidden": False,
        "note": "",
        "groups": [],
    }


class TestDashboard(unittest.TestCase):
    def _call(self, raw, stats, **kw):
        with (
            patch.object(workbench, "_ensure_projects_migrated", lambda: None),
            patch.object(workbench.person_store, "list_persons", return_value=raw),
            patch.object(workbench.person_store, "dynasty_options", return_value=["魏晋"]),
            patch.object(workbench.person_store, "category_options", return_value=["政治家"]),
            patch.object(workbench.task_repo, "counts_by_person", return_value=stats) as counts,
        ):
            resp = workbench.persons_dashboard(_Req(), **kw)
        return resp["data"], counts


class TestSingleAggregation(TestDashboard):
    def test_counts_called_once_regardless_of_person_count(self):
        raw = [_person(f"P{i}", doc_count=10 - i) for i in range(50)]
        data, counts = self._call(raw, {})
        self.assertEqual(counts.call_count, 1)  # 关键：绝不做逐人查询
        self.assertEqual(data["total"], 50)

    def test_stats_merged_onto_cards(self):
        raw = [_person("曹操", dynasty="魏晋", doc_count=223)]
        stats = {"曹操": {"video_count": 5, "article_count": 12,
                         "article_enabled_count": 9, "active_task_count": 2,
                         "last_updated": "2026-10-14T00:00:00Z"}}
        data, _ = self._call(raw, stats)
        card = next(p for p in data["persons"] if p["person"] == "曹操")
        self.assertEqual(card["video_count"], 5)
        self.assertEqual(card["article_count"], 12)
        self.assertEqual(card["article_enabled_count"], 9)
        self.assertEqual(card["active_task_count"], 2)
        self.assertTrue(card["used"])
        self.assertEqual(data["active_task_total"], 2)


class TestCandidateExclusion(TestDashboard):
    def test_failed_task_person_not_candidate(self):
        # 曹操有失败任务（stats 里有键但未用）→ 不能出现在"尚未使用"候选里。
        raw = [
            _person("曹操", doc_count=223),
            _person("刘备", doc_count=192),
        ]
        stats = {"曹操": {"video_count": 0, "article_count": 0,
                         "article_enabled_count": 0, "active_task_count": 0,
                         "last_updated": "2026-10-14T00:00:00Z"}}
        data, _ = self._call(raw, stats)
        cand_names = {c["person"] for c in data["candidates"]}
        self.assertNotIn("曹操", cand_names)
        self.assertIn("刘备", cand_names)

    def test_status_filter_active(self):
        raw = [_person("曹操"), _person("刘备")]
        stats = {"曹操": {"video_count": 0, "article_count": 0,
                         "article_enabled_count": 0, "active_task_count": 1,
                         "last_updated": None}}
        data, _ = self._call(raw, stats, status="active")
        self.assertEqual([p["person"] for p in data["persons"]], ["曹操"])


class TestActiveDrawer(unittest.TestCase):
    def test_draft_and_terminated_excluded(self):
        rows = [
            {"task_id": "a", "status": "draft", "person_name": "X"},
            {"task_id": "b", "status": "processing", "person_name": "Y"},
            {"task_id": "c", "status": "completed", "person_name": "Z"},
        ]
        with (
            patch.object(workbench, "_ensure_projects_migrated", lambda: None),
            patch.object(task_repo, "list_tasks", return_value=rows),
            patch.object(workbench.sm.state, "get_task", return_value=None),
        ):
            resp = workbench.active_tasks(_Req())
        ids = {t["task_id"] for t in resp["data"]["tasks"]}
        self.assertEqual(ids, {"b"})  # 只保留进行中，草稿/完成被过滤


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
