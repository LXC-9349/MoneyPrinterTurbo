"""工作台仪表盘/候选端点的回归测试（里程碑 1 + 先修1，§二/§七）。

直接调用控制器函数（纯逻辑 + 依赖打桩），不建真实 DB / 语料 / HTTP，避免触碰
959MB 史料库与 config.toml。锁定不变量：
1. 聚合统计单次获取：counts_by_person 只被调用一次，杜绝逐人 N+1。
2. 主网格只显示“已使用”的人物（有任务/文章/置顶/自建/采纳/备注/分组任一）。
3. 候选（史料库有语料但未使用）改为服务端搜索 + 分页（≤50），有任意任务/文章
   历史（含失败）的人物不进入候选。
4. 进行中抽屉不含草稿：/tasks/active 只返回 ACTIVE_STATUSES。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.controllers.v1 import workbench
from app.services import task_repo


class _Req:  # 端点只用 request 占位，不需要真实内容。
    pass


def _person(
    name,
    *,
    dynasty="",
    source="library",
    doc_count=0,
    pinned=False,
    adopted=False,
    note="",
    groups=None,
    hidden=False,
):
    return {
        "person": name,
        "dynasty": dynasty,
        "category": "",
        "doc_count": doc_count,
        "source": source,
        "pinned": pinned,
        "hidden": hidden,
        "note": note,
        "groups": groups or [],
        "adopted": adopted,
    }


def _stats(video=0, article=0, active=0, task=0, awaiting=0, last=None):
    return {
        "task_count": task,
        "video_count": video,
        "article_count": article,
        "article_enabled_count": 0,
        "active_task_count": active,
        "awaiting_count": awaiting,
        "last_updated": last,
    }


class TestDashboard(unittest.TestCase):
    def _call(self, raw, stats, **kw):
        # 直连调用不经过 FastAPI 校验，补齐字符串/整型默认值以贴合真实入参。
        for k, v in (
            ("keyword", ""),
            ("dynasty", ""),
            ("category", ""),
            ("group", ""),
            ("status", ""),
            ("sort", ""),
        ):
            kw.setdefault(k, v)
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
        # 50 位里只有 P0 有任务（已使用），其余为候选（不进主网格）。
        raw = [_person(f"P{i}", doc_count=10) for i in range(50)]
        stats = {"P0": _stats(task=1, video=1)}
        data, counts = self._call(raw, stats)
        self.assertEqual(counts.call_count, 1)  # 关键：绝不做逐人查询

    def test_grid_only_used_persons(self):
        raw = [
            _person("曹操", doc_count=223),
            _person("刘备", doc_count=192),
            _person("自建者", source="custom"),
            _person("被采纳", doc_count=5, adopted=True),
            _person("有备注", doc_count=5, note="关注"),
            _person("纯候选", doc_count=5),
        ]
        stats = {"曹操": _stats(task=1, video=1, article=3, active=2)}
        data, _ = self._call(raw, stats)
        names = [p["person"] for p in data["persons"]]
        # 已使用：曹操(有任务)、自建者(custom)、被采纳(adopted)、有备注(note)。
        self.assertEqual(set(names), {"曹操", "自建者", "被采纳", "有备注"})
        self.assertNotIn("纯候选", names)
        self.assertNotIn("刘备", names)
        # 候选计数：史料库有语料且未使用（刘备 + 纯候选）。
        self.assertEqual(data["candidate_count"], 2)
        self.assertEqual(data["total"], 4)

    def test_stats_merged_onto_cards(self):
        raw = [_person("曹操", dynasty="魏晋", doc_count=223)]
        stats = {"曹操": _stats(video=5, article=12, active=2, task=20, last="2026-10-14T00:00:00Z")}
        data, _ = self._call(raw, stats)
        card = next(p for p in data["persons"] if p["person"] == "曹操")
        self.assertEqual(card["video_count"], 5)
        self.assertEqual(card["article_count"], 12)
        self.assertEqual(card["active_task_count"], 2)
        self.assertEqual(card["task_count"], 20)
        self.assertTrue(card["used"])
        self.assertEqual(data["active_task_total"], 2)


class TestCandidatesEndpoint(TestDashboard):
    def _call_cand(self, raw, stats, **kw):
        for k, v in (("keyword", ""), ("dynasty", ""), ("category", ""), ("group", "")):
            kw.setdefault(k, v)
        kw.setdefault("offset", 0)
        kw.setdefault("limit", 50)
        with (
            patch.object(workbench, "_ensure_projects_migrated", lambda: None),
            patch.object(workbench.person_store, "list_persons", return_value=raw),
            patch.object(workbench.task_repo, "counts_by_person", return_value=stats),
        ):
            resp = workbench.persons_candidates(_Req(), **kw)
        return resp["data"]

    def test_failed_task_person_not_candidate(self):
        # 曹操有失败任务（task_count>0）→ 已使用，不能出现在候选里。
        raw = [_person("曹操", doc_count=223), _person("刘备", doc_count=192)]
        stats = {"曹操": _stats(task=1)}
        data = self._call_cand(raw, stats)
        names = {c["person"] for c in data["candidates"]}
        self.assertNotIn("曹操", names)
        self.assertIn("刘备", names)
        self.assertEqual(data["total"], 1)

    def test_pagination_limit_capped_at_50(self):
        raw = [_person(f"P{i}", doc_count=200 - i) for i in range(120)]
        data = self._call_cand(raw, {}, offset=0, limit=200)
        self.assertLessEqual(len(data["candidates"]), 50)  # 硬上限 50
        self.assertEqual(data["total"], 120)
        self.assertTrue(data["has_more"])

    def test_offset_slices(self):
        raw = [_person(f"P{i}", doc_count=100 - i) for i in range(5)]
        data = self._call_cand(raw, {}, offset=3, limit=50)
        self.assertEqual([c["person"] for c in data["candidates"]], ["P3", "P4"])
        self.assertFalse(data["has_more"])


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
