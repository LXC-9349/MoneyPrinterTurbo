"""传记向导拆出的基础脚本纯函数测试（不依赖 Streamlit 运行时）。

向导第②步的“默认全选”由每篇 checkbox 的 value=True 实现，其背后的
doc_id 提取与判定逻辑收敛在 library.py，这里直接测这些纯函数。
"""
import unittest

from app.services import library


class IsBiographySourceTest(unittest.TestCase):
    def test_only_historical_archive_is_biography(self):
        self.assertTrue(library.is_biography_source("historical_archive"))
        self.assertTrue(library.is_biography_source("  historical_archive  "))
        self.assertFalse(library.is_biography_source("pexels"))
        self.assertFalse(library.is_biography_source(""))
        self.assertFalse(library.is_biography_source(None))


class AllDocIdsTest(unittest.TestCase):
    def test_dedup_keep_order_and_skip_blank(self):
        profile = {
            "docs": [
                {"doc_id": "d-1"},
                {"doc_id": "d-2"},
                {"doc_id": "d-1"},  # 重复，只保留一次
                {"doc_id": "   "},  # 纯空白，丢弃
                {"doc_id": None},  # None，丢弃
                {},  # 缺 doc_id，丢弃
                {"doc_id": 12345},  # 非字符串，转成 str 保留
            ]
        }
        self.assertEqual(library.all_doc_ids(profile), ["d-1", "d-2", "12345"])

    def test_handles_missing_profile_and_docs(self):
        self.assertEqual(library.all_doc_ids(None), [])
        self.assertEqual(library.all_doc_ids({}), [])
        self.assertEqual(library.all_doc_ids({"docs": None}), [])
        self.assertEqual(library.all_doc_ids({"docs": []}), [])


class DefaultBaseScriptIdsTest(unittest.TestCase):
    def test_auto_select_equals_all_docs(self):
        profile = {"docs": [{"doc_id": "a"}, {"doc_id": "b"}, {"doc_id": "c"}]}
        # 默认＝自动全选该人物全部传记文章。
        self.assertEqual(library.default_base_script_ids(profile), ["a", "b", "c"])
        self.assertEqual(
            library.default_base_script_ids(profile),
            library.all_doc_ids(profile),
        )

    def test_empty_profile_yields_empty_selection(self):
        self.assertEqual(library.default_base_script_ids(None), [])
        self.assertEqual(library.default_base_script_ids({"docs": []}), [])


if __name__ == "__main__":
    unittest.main()
