"""史料库文章软引用行与计数（里程碑 2 收尾 #6）。

用临时合成文库（env 隔离四件套）验证：
- library 软查询：top_docs 截断（默认 12）、可信度映射、title LIKE 兜底；
- workbench._soft_library_articles：硬添加排除、低可信默认不启用、library_links 已设值优先；
- person_detail 聚合：library_articles 与 summary 四计数；
- PATCH /persons/{name}/library-links/{doc_id}：参数校验/404/落库/未物化自动建档；
- _merge_soft_counts：卡片 article_count 合并软引、used 不受影响；
- db 迁移 3：旧库 library_links 条件补列且数据不丢。
"""

from __future__ import annotations

import gc
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.controllers.v1 import workbench
from app.models.exception import HttpException
from app.models.schema import LibraryLinkPatchRequest
from app.services import article_repo, db_migrations, library, person_repo


def _req():
    return SimpleNamespace(headers={"x-task-id": "req-1"})


def _make_library_db(path: Path) -> None:
    """合成迷你文库：曹操 14 篇 top_docs（测截 12），李世民 1 篇，好的仙人无 persons 行走 LIKE 兜底。"""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE documents (
            doc_id TEXT PRIMARY KEY, source TEXT NOT NULL, category TEXT,
            title TEXT NOT NULL, path TEXT NOT NULL, body TEXT NOT NULL,
            n_chars INTEGER NOT NULL, extracted_at TEXT
        );
        CREATE TABLE books (
            book_id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL,
            author TEXT DEFAULT '', path TEXT NOT NULL, format TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', n_chapters INTEGER DEFAULT 0,
            n_chars INTEGER DEFAULT 0, error TEXT DEFAULT '', processed_at TEXT
        );
        CREATE TABLE annotations (
            target_type TEXT NOT NULL, target_id TEXT NOT NULL, json TEXT NOT NULL,
            model TEXT, annotated_at TEXT, PRIMARY KEY (target_type, target_id)
        );
        CREATE TABLE persons (
            person TEXT PRIMARY KEY, dynasty TEXT, doc_count INTEGER,
            reliable_high INTEGER, facts_json TEXT, top_docs TEXT, books TEXT
        );
        """
    )
    docs = [(f"d-{i}", "风云人物", f"曹操事迹{i}") for i in range(1, 15)]
    docs.append(("d-20", "文史百科", "贞观之治"))
    docs.append(("d-30", "野史秘闻", "好的仙人别传"))
    for doc_id, category, title in docs:
        conn.execute(
            "INSERT INTO documents VALUES (?, 'A', ?, ?, '/x', 'body', 4, NULL)",
            (doc_id, category, title),
        )
    # 前三篇显式标可信度；d-4 起不标注 → 软行按 medium；d-20 高；d-30 低。
    rel = {"d-1": "高", "d-2": "中", "d-3": "低", "d-20": "高", "d-30": "低"}
    for doc_id, r in rel.items():
        payload = json.dumps({"persons": [], "key_facts": [], "reliable": r}, ensure_ascii=False)
        conn.execute("INSERT INTO annotations VALUES ('doc', ?, ?, NULL, NULL)", (doc_id, payload))
    rows = [
        ("曹操", "秦汉", 14, 1, [], [f"d-{i}" for i in range(1, 15)], []),
        ("李世民", "唐", 1, 1, [], ["d-20"], []),
    ]
    conn.executemany(
        "INSERT INTO persons VALUES (?,?,?,?,?,?,?)",
        [
            (p, d, n, h, json.dumps(f, ensure_ascii=False), json.dumps(t, ensure_ascii=False), "[]")
            for p, d, n, h, f, t, _ in rows
        ],
    )
    conn.commit()
    conn.close()


class _SoftCase(unittest.TestCase):
    """公共 harness：合成文库 + mpt 临时库 + 文章目录 + 个人库全部 env 隔离。"""

    def setUp(self):
        self._td = Path(tempfile.mkdtemp(prefix="mpt-soft-"))
        _make_library_db(self._td / "library.sqlite")
        self._env = patch.dict(
            os.environ,
            {
                "MPT_MAIN_DB": str(self._td / "mpt.sqlite"),
                "MPT_ARTICLES_DIR": str(self._td / "articles"),
                "MPT_PERSONAL_DB": str(self._td / "personal.sqlite"),
                "MPT_LIBRARY_DB": str(self._td / "library.sqlite"),
            },
        )
        self._env.start()
        c = person_repo.connect()
        c.close()
        self._patch = patch.object(workbench, "_ensure_projects_migrated", lambda: None)
        self._patch.start()
        self._enabled = patch.object(library, "enabled", lambda: True)
        self._enabled.start()
        # Windows：sqlite 句柄随 GC 才释放，先清缓存/回收再删目录。
        self.addCleanup(shutil.rmtree, self._td, ignore_errors=True)
        self.addCleanup(self._teardown_extra)

    def _teardown_extra(self):
        self._enabled.stop()
        self._patch.stop()
        self._env.stop()
        library._conn.cache_clear()
        gc.collect()


class TestLibrarySoftQueries(unittest.TestCase):
    def setUp(self):
        self._td = Path(tempfile.mkdtemp(prefix="mpt-soft-q-"))
        _make_library_db(self._td / "library.sqlite")
        self.enterContext(
            patch.dict(os.environ, {"MPT_LIBRARY_DB": str(self._td / "library.sqlite")})
        )
        self.enterContext(patch.object(library, "enabled", lambda: True))
        self.addCleanup(library._conn.cache_clear)
        self.addCleanup(shutil.rmtree, self._td, ignore_errors=True)
        self.addCleanup(gc.collect)

    def test_truncation_and_reliability_mapping(self):
        rows = library.person_soft_articles("曹操")
        self.assertEqual(len(rows), 12, "top_docs 14 篇应截断为 12 篇")
        self.assertEqual([r["doc_id"] for r in rows], [f"d-{i}" for i in range(1, 13)])
        by_id = {r["doc_id"]: r for r in rows}
        self.assertEqual(by_id["d-1"]["reliability"], "high")
        self.assertEqual(by_id["d-2"]["reliability"], "medium")
        self.assertEqual(by_id["d-3"]["reliability"], "low")
        # 未标注的按 medium 兜底
        self.assertEqual(by_id["d-5"]["reliability"], "medium")
        self.assertEqual(by_id["d-1"]["title"], "曹操事迹1")
        self.assertEqual(by_id["d-1"]["category"], "风云人物")
        self.assertEqual(by_id["d-1"]["n_chars"], 4)

    def test_max_docs_override(self):
        self.assertEqual(len(library.person_soft_articles("曹操", max_docs=20)), 14)

    def test_title_like_fallback(self):
        # persons 无“好的仙人”行 → 退化 title LIKE，与档案口径一致
        rows = library.person_soft_articles("好的仙人")
        self.assertEqual([r["doc_id"] for r in rows], ["d-30"])
        self.assertEqual(rows[0]["reliability"], "low")

    def test_empty_name_and_unknown_person(self):
        self.assertEqual(library.person_soft_articles(""), [])
        self.assertEqual(library.person_soft_articles("查无此人"), [])

    def test_soft_docs_for_names_batch(self):
        out = library.soft_docs_for_names(["曹操", "李世民", "曹操", ""])
        self.assertEqual(set(out), {"曹操", "李世民"})
        self.assertEqual(len(out["曹操"]), 12)
        self.assertEqual([r["doc_id"] for r in out["李世民"]], ["d-20"])
        self.assertEqual(out["李世民"][0]["reliability"], "high")

    def test_library_db_missing_degrades_empty(self):
        with patch.dict(os.environ, {"MPT_LIBRARY_DB": str(self._td / "missing.sqlite")}):
            library._conn.cache_clear()
            self.assertEqual(library.person_soft_articles("曹操"), [])
            self.assertEqual(library.soft_docs_for_names(["曹操"]), {})


class TestSoftRowsAndLinkStates(_SoftCase):
    def test_get_link_states_unmaterialized_empty(self):
        self.assertEqual(person_repo.get_link_states("曹操"), {})

    def test_upsert_validation(self):
        with self.assertRaises(ValueError):
            person_repo.upsert_link_state("", "d-1")
        with self.assertRaises(ValueError):
            person_repo.upsert_link_state("曹操", "  ")

    def test_upsert_creates_person_and_only_touches_given_fields(self):
        row = person_repo.upsert_link_state("曹操", "d-3", enabled=True, source_title="曹操事迹3")
        self.assertEqual(row["enabled"], 1)
        self.assertIsNone(row["weight"])
        row = person_repo.upsert_link_state("曹操", "d-3", weight=2.5)
        self.assertEqual(row["enabled"], 1, "只传 weight 不应清掉已设 enabled")
        self.assertAlmostEqual(row["weight"], 2.5)
        # 未物化人物自动建档后可按主名解析
        states = person_repo.get_link_states("曹操")
        self.assertIn("d-3", states)
        self.assertEqual(states["d-3"]["enabled"], 1)

    def test_bulk_link_states_multi_person(self):
        person_repo.upsert_link_state("曹操", "d-3", enabled=False)
        person_repo.upsert_link_state("李世民", "d-20", enabled=True, weight=0.5)
        out = person_repo.bulk_link_states(["曹操", "李世民", "无名"])
        self.assertEqual(out[("曹操", "d-3")]["enabled"], 0)
        self.assertEqual(out[("李世民", "d-20")]["enabled"], 1)
        self.assertAlmostEqual(out[("李世民", "d-20")]["weight"], 0.5)
        self.assertEqual(person_repo.bulk_link_states([]), {})

    def test_soft_library_articles_excludes_hard_and_defaults(self):
        article_repo.add_article("曹操", "曹操事迹1", source="library", content_ref="library:d-1")
        articles = article_repo.list_articles("曹操")
        rows = workbench._soft_library_articles("曹操", articles)
        ids = [r["doc_id"] for r in rows]
        self.assertNotIn("d-1", ids, "已硬添加的 doc 应从软引用排除避免双计")
        self.assertEqual(len(rows), 11)
        by_id = {r["doc_id"]: r for r in rows}
        self.assertFalse(by_id["d-3"]["enabled"], "低可信未设置默认不启用")
        self.assertTrue(by_id["d-2"]["enabled"])
        self.assertAlmostEqual(by_id["d-3"]["weight"], 1.0, "未设置权重默认 1.0")
        # 已设值优先于推导
        person_repo.upsert_link_state("曹操", "d-3", enabled=True)
        rows = workbench._soft_library_articles("曹操", articles)
        self.assertTrue({r["doc_id"]: r for r in rows}["d-3"]["enabled"])

    def test_soft_library_articles_no_library_rows(self):
        self.assertEqual(workbench._soft_library_articles("查无此人", []), [])

    def test_library_doc_ids_by_person(self):
        article_repo.add_article("曹操", "x", source="library", content_ref="library:d-1")
        article_repo.add_article("曹操", "y", source="paste", content="正文")
        out = article_repo.library_doc_ids_by_person()
        self.assertEqual(out, {"曹操": {"d-1"}})


class TestDetailAndPatchEndpoints(_SoftCase):
    def _profile(self, name="曹操"):
        return {"person": name, "dynasty": "秦汉", "key_facts": [], "docs": []}

    def test_detail_merges_soft_rows_into_summary(self):
        article_repo.add_article("曹操", "文", source="paste", content="内容")
        with patch.object(workbench.person_store, "get_person", return_value=self._profile()):
            resp = workbench.person_detail(_req(), "曹操")["data"]
        la = resp["library_articles"]
        self.assertEqual(len(la), 12)
        s = resp["summary"]
        self.assertEqual(s["library_article_count"], 12)
        self.assertEqual(s["library_article_enabled_count"], 11, "d-3 低可信默认不启用")
        self.assertEqual(s["article_count"], 13, "硬 1 + 软 12")
        self.assertEqual(s["article_enabled_count"], 12, "硬启用 1 + 软启用 11")

    def test_detail_hard_added_not_double_counted(self):
        article_repo.add_article("曹操", "曹操事迹1", source="library", content_ref="library:d-1")
        with patch.object(workbench.person_store, "get_person", return_value=self._profile()):
            resp = workbench.person_detail(_req(), "曹操")["data"]
        s = resp["summary"]
        self.assertEqual(len(resp["library_articles"]), 11)
        self.assertEqual(s["article_count"], 12, "硬 1(=d-1) + 软 11，不双计")

    def test_patch_toggle_and_weight(self):
        with patch.object(workbench.person_store, "get_person", return_value=self._profile()):
            out = workbench.patch_library_link(
                _req(), "曹操", "d-3", LibraryLinkPatchRequest(enabled=True)
            )["data"]
            self.assertTrue(out["enabled"])
            self.assertIsNone(out["weight"])
            workbench.patch_library_link(
                _req(), "曹操", "d-3", LibraryLinkPatchRequest(weight=1.8)
            )
        st = person_repo.get_link_states("曹操")["d-3"]
        self.assertEqual(st["enabled"], 1)
        self.assertAlmostEqual(st["weight"], 1.8)

    def test_patch_requires_enabled_or_weight(self):
        with patch.object(workbench.person_store, "get_person", return_value=self._profile()):
            with self.assertRaises(HttpException) as raised:
                workbench.patch_library_link(
                    _req(), "曹操", "d-3", LibraryLinkPatchRequest()
                )
        self.assertEqual(raised.exception.status_code, 400)

    def test_patch_unknown_doc_and_person_404(self):
        with patch.object(workbench.person_store, "get_person", return_value=self._profile()):
            with self.assertRaises(HttpException) as raised:
                workbench.patch_library_link(
                    _req(), "曹操", "d-999", LibraryLinkPatchRequest(enabled=True)
                )
            self.assertEqual(raised.exception.status_code, 404)
            # 截断外的 top_docs（d-13/d-14）同样不在软引用列表
            with self.assertRaises(HttpException) as raised2:
                workbench.patch_library_link(
                    _req(), "曹操", "d-14", LibraryLinkPatchRequest(enabled=True)
                )
            self.assertEqual(raised2.exception.status_code, 404)
        with patch.object(workbench.person_store, "get_person", return_value=None):
            with self.assertRaises(HttpException) as raised3:
                workbench.patch_library_link(
                    _req(), "查无此人", "d-1", LibraryLinkPatchRequest(enabled=True)
                )
        self.assertEqual(raised3.exception.status_code, 404)

    def test_patch_records_source_title(self):
        with patch.object(workbench.person_store, "get_person", return_value=self._profile()):
            workbench.patch_library_link(
                _req(), "曹操", "d-2", LibraryLinkPatchRequest(enabled=False)
            )
        st = person_repo.get_link_states("曹操")["d-2"]
        self.assertEqual(st["enabled"], 0)


class TestMergeSoftCounts(_SoftCase):
    def test_card_counts_merged_and_used_unchanged(self):
        items = [
            {"person": "曹操", "article_count": 2, "article_enabled_count": 1, "used": True},
            {"person": "李世民", "article_count": 0, "article_enabled_count": 0, "used": False},
            {"person": "查无此人", "article_count": 0, "article_enabled_count": 0, "used": False},
        ]
        workbench._merge_soft_counts(items)
        cc = items[0]
        self.assertEqual(cc["article_count"], 14, "硬 2 + 软 12")
        self.assertEqual(cc["article_enabled_count"], 12, "硬启用 1 + 软启用 11")
        self.assertEqual(cc["library_article_count"], 12)
        self.assertEqual(cc["library_article_enabled_count"], 11)
        self.assertTrue(cc["used"], "used 判定不受软引用合并影响")
        self.assertEqual(items[1]["article_count"], 1)
        self.assertEqual(items[1]["library_article_enabled_count"], 1)
        # 无软引用的卡片不加新字段、计数不变
        self.assertNotIn("library_article_count", items[2])
        self.assertEqual(items[2]["article_count"], 0)

    def test_hard_added_excluded_from_merge(self):
        article_repo.add_article("曹操", "x", source="library", content_ref="library:d-1")
        items = [{"person": "曹操", "article_count": 1, "article_enabled_count": 1}]
        workbench._merge_soft_counts(items)
        self.assertEqual(items[0]["library_article_count"], 11, "d-1 已硬添加不计入软引")

    def test_override_state_respects_in_merge(self):
        person_repo.upsert_link_state("曹操", "d-2", enabled=False)
        person_repo.upsert_link_state("曹操", "d-3", enabled=True)
        items = [{"person": "曹操", "article_count": 0, "article_enabled_count": 0}]
        workbench._merge_soft_counts(items)
        self.assertEqual(items[0]["library_article_count"], 12)
        # 默认启用 11 → d-2 关、d-3 开 → 仍 11
        self.assertEqual(items[0]["library_article_enabled_count"], 11)

    def test_empty_items_noop(self):
        workbench._merge_soft_counts([])


class TestMigrationV3(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_file = os.path.join(self._tmp.name, "mpt.sqlite")
        self.backups_dir = os.path.join(self._tmp.name, "backups")

    def tearDown(self):
        self._tmp.cleanup()

    def test_apply_v3_adds_columns_and_preserves_rows(self):
        # 手工构造迁移 2 的旧库：library_links 无 enabled/weight 列且带数据。
        conn = db_migrations.connect(self.db_file)
        conn.executescript(
            """
            CREATE TABLE persons (
              person_id INTEGER PRIMARY KEY AUTOINCREMENT,
              public_id TEXT NOT NULL UNIQUE,
              display_name TEXT NOT NULL,
              qid TEXT NOT NULL DEFAULT '',
              dynasty TEXT NOT NULL DEFAULT '',
              category TEXT NOT NULL DEFAULT '',
              note TEXT NOT NULL DEFAULT '',
              origin TEXT NOT NULL DEFAULT 'library',
              state TEXT NOT NULL DEFAULT 'active',
              merged_into INTEGER,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE library_links (
              person_id INTEGER NOT NULL,
              doc_id TEXT NOT NULL,
              book_id TEXT NOT NULL DEFAULT '',
              role TEXT NOT NULL DEFAULT 'source',
              source_title TEXT NOT NULL DEFAULT '',
              source_page TEXT NOT NULL DEFAULT '',
              PRIMARY KEY (person_id, doc_id)
            );
            """
        )
        conn.execute(
            "INSERT INTO persons(public_id,display_name,created_at,updated_at)"
            " VALUES('per_1','曹操','t','t')"
        )
        conn.execute(
            "INSERT INTO library_links(person_id,doc_id) VALUES(1,'d-1')"
        )
        conn.execute("PRAGMA user_version=2")
        conn.commit()
        try:
            v = db_migrations.ensure_migrated(conn, backups_dir=self.backups_dir)
            self.assertEqual(v, db_migrations.TARGET_VERSION)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(library_links)")}
            self.assertIn("enabled", cols)
            self.assertIn("weight", cols)
            row = conn.execute(
                "SELECT enabled, weight FROM library_links WHERE person_id=1 AND doc_id='d-1'"
            ).fetchone()
            self.assertIsNone(row["enabled"], "旧行新列应为 NULL=未设置")
            self.assertIsNone(row["weight"])
            # 再跑一遍幂等
            db_migrations.ensure_migrated(conn, backups_dir=self.backups_dir)
        finally:
            conn.close()

    def test_fresh_db_has_columns_via_schema_v1(self):
        conn = db_migrations.connect(":memory:")
        try:
            db_migrations.ensure_migrated(conn)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(library_links)")}
            self.assertIn("enabled", cols)
            self.assertIn("weight", cols)
        finally:
            conn.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
