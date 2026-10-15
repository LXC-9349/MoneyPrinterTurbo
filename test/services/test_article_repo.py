"""article_repo（里程碑 2 · 人物详情页“文章”标签）的测试。

全部走临时目录 + 合成数据：连接经 person_repo.connect 建表并前滚到含 articles 的
schema；正文落盘目录用 env `MPT_ARTICLES_DIR` 隔离到临时目录，不触真实 storage。
锁定契约：
* paste/upload 落盘正文，excerpt/word_count 推导，read_content 往返一致。
* library 软引用 content_ref=`library:<doc_id>`，无本地文件。
* 低可信度（reliability=low）默认不启用；显式 enabled 覆盖默认。
* set_enabled / set_weight / delete（含删文件）/ enabled_for_generation 权重排序。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from app.services import article_repo, person_repo


class _Case(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._td.name, "mpt.sqlite")
        self.articles = os.path.join(self._td.name, "articles")
        self._env = patch.dict(os.environ, {"MPT_ARTICLES_DIR": self.articles})
        self._env.start()
        self.conn = person_repo.connect(self.db)

    def tearDown(self):
        self.conn.close()
        self._env.stop()
        self._td.cleanup()


class TestAddAndRead(_Case):
    def test_paste_lands_file_and_roundtrips(self):
        art = article_repo.add_article(
            "曹操", "武帝纪", source="paste", content="曹操，字孟德，一名吉利。" * 30,
            conn=self.conn,
        )
        self.assertEqual(art["person_name"], "曹操")
        self.assertEqual(art["source"], "paste")
        self.assertEqual(art["enabled"], 1)  # 用户粘贴默认启用
        self.assertGreater(art["word_count"], 0)
        self.assertTrue(art["excerpt"])
        # 正文文件确实落盘。
        self.assertTrue(os.path.isfile(os.path.join(self.articles, art["content_ref"])))
        # read_content 往返一致。
        full = article_repo.read_content(art)
        self.assertIn("曹操，字孟德", full)

    def test_library_soft_reference_no_file(self):
        art = article_repo.add_article(
            "曹操", "史料标题", source="library",
            content_ref="library:doc_123", excerpt="节选摘要",
            conn=self.conn,
        )
        self.assertEqual(art["content_ref"], "library:doc_123")
        self.assertFalse(os.path.exists(os.path.join(self.articles, art["content_ref"])))
        # 文库不可用时读回 excerpt。
        self.assertEqual(article_repo.read_content(art), "节选摘要")


class TestReliabilityDefaults(_Case):
    def test_low_reliability_defaults_disabled(self):
        art = article_repo.add_article(
            "曹操", "存疑来源", source="upload", content="正文内容",
            reliability="low", conn=self.conn,
        )
        self.assertEqual(art["enabled"], 0)

    def test_explicit_enabled_overrides_default(self):
        art = article_repo.add_article(
            "曹操", "存疑但手动启用", source="upload", content="正文",
            reliability="low", enabled=True, conn=self.conn,
        )
        self.assertEqual(art["enabled"], 1)


class TestToggleWeightDelete(_Case):
    def test_set_enabled_and_weight(self):
        art = article_repo.add_article(
            "曹操", "文", source="paste", content="内容内容", conn=self.conn,
        )
        off = article_repo.set_enabled(art["article_id"], False, conn=self.conn)
        self.assertEqual(off["enabled"], 0)
        w = article_repo.set_weight(art["article_id"], 2.5, conn=self.conn)
        self.assertAlmostEqual(w["weight"], 2.5)

    def test_delete_removes_row_and_file(self):
        art = article_repo.add_article(
            "曹操", "文", source="paste", content="内容", conn=self.conn,
        )
        path = os.path.join(self.articles, art["content_ref"])
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(article_repo.delete_article(art["article_id"], conn=self.conn))
        self.assertIsNone(article_repo.get_article(art["article_id"], conn=self.conn))
        self.assertFalse(os.path.isfile(path))  # 文件一并删除

    def test_delete_library_ref_keeps_no_file(self):
        art = article_repo.add_article(
            "曹操", "史", source="library", content_ref="library:d1",
            excerpt="e", conn=self.conn,
        )
        self.assertTrue(article_repo.delete_article(art["article_id"], conn=self.conn))


class TestEnabledForGeneration(_Case):
    def test_only_enabled_sorted_by_weight_desc(self):
        hi = article_repo.add_article(
            "曹操", "高权重", source="paste", content="a", weight=3.0, conn=self.conn,
        )
        lo = article_repo.add_article(
            "曹操", "低权重", source="paste", content="b", weight=1.0, conn=self.conn,
        )
        off = article_repo.add_article(
            "曹操", "停用", source="paste", content="c", enabled=False, conn=self.conn,
        )
        gen = article_repo.enabled_for_generation("曹操", conn=self.conn)
        ids = [g["article_id"] for g in gen]
        self.assertNotIn(off["article_id"], ids)
        self.assertEqual(ids[0], hi["article_id"])  # 权重高的在前
        self.assertIn(lo["article_id"], ids)

    def test_touch_used_sets_timestamp(self):
        art = article_repo.add_article(
            "曹操", "文", source="paste", content="x", conn=self.conn,
        )
        self.assertIsNone(art["last_used_at"])
        article_repo.touch_used(art["article_id"], conn=self.conn)
        self.assertIsNotNone(
            article_repo.get_article(art["article_id"], conn=self.conn)["last_used_at"]
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
