"""M4#4 效果预览服务：分镜预览（纯函数、无网络）+ 试听裁剪 + 参数哈希缓存。

全合成：archive 检索与门槛上下文用 mock 替身，只验证 preview.py 的编排、
相关性口径、覆盖率/独立素材数统计、缺素材警告与哈希缓存。
"""
import tempfile
import unittest
from unittest import mock

from app.models.schema import MaterialInfo
from app.services import preview as pv


def _item(provider, asset_id, url, *, license="public domain", author=""):
    it = MaterialInfo()
    it.provider = provider
    it.url = url
    it.duration = 0
    it.source_info = {
        "provider": provider,
        "asset_id": asset_id,
        "source_page": f"https://commons/{asset_id}",
        "credit": author,
        "search_term": "",
        "license": license,
        "license_url": "",
        "creator": {"name": author},
    }
    return it


class TestParamsHash(unittest.TestCase):
    def test_stable_across_key_order(self):
        self.assertEqual(
            pv.params_hash({"a": 1, "b": 2}),
            pv.params_hash({"b": 2, "a": 1}),
        )

    def test_differs_on_content(self):
        self.assertNotEqual(
            pv.params_hash({"person": "曹操"}),
            pv.params_hash({"person": "李世民"}),
        )


class TestCache(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        patcher = mock.patch(
            "app.utils.utils.storage_dir", return_value=self._td.name
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._td.cleanup)

    def test_roundtrip(self):
        pv.cache_put("k1", {"hello": "世界"})
        self.assertEqual(pv.cache_get("k1"), {"hello": "世界"})

    def test_missing_key_returns_none(self):
        self.assertIsNone(pv.cache_get("nope"))

    def test_expired_returns_none(self):
        pv.cache_put("k2", {"v": 1})
        with mock.patch.object(pv, "CACHE_TTL_SEC", -1):
            self.assertIsNone(pv.cache_get("k2"))


class TestStoryboardPreview(unittest.TestCase):
    def setUp(self):
        ctx_patch = mock.patch.object(
            pv.archive,
            "person_gate_context",
            return_value={
                "name": "曹操",
                "aliases": ["Cao Cao"],
                "era_keywords": ["Three Kingdoms"],
            },
        )
        ctx_patch.start()
        self.addCleanup(ctx_patch.stop)
        excl_patch = mock.patch(
            "app.services.person_repo.excluded_asset_keys", return_value=set()
        )
        excl_patch.start()
        self.addCleanup(excl_patch.stop)
        # 缓存关闭，避免测试间串味。
        self._td = tempfile.TemporaryDirectory()
        sd_patch = mock.patch(
            "app.utils.utils.storage_dir", return_value=self._td.name
        )
        sd_patch.start()
        self.addCleanup(sd_patch.stop)
        self.addCleanup(self._td.cleanup)

    def test_relevant_shot_counts_coverage(self):
        good = _item("wikimedia", "File:Cao_Cao_portrait.jpg", "https://x/a.jpg")
        with mock.patch.object(
            pv.archive, "find_person_images", return_value=[good]
        ):
            res = pv.storyboard_preview(
                "曹操",
                [{"narration": "曹操，字孟德。", "visual_query": "portrait", "visual_type": "historical_photo"}],
                threshold="loose",
                use_cache=False,
            )
        self.assertEqual(res["shot_count"], 1)
        self.assertEqual(res["coverage"], 1.0)
        self.assertGreaterEqual(res["shots"][0]["relevant_count"], 1)
        self.assertEqual(res["shots"][0]["independent_assets"], 1)
        self.assertEqual(res["missing_shots"], [])

    def test_missing_shot_warns_not_silent(self):
        with mock.patch.object(pv.archive, "find_person_images", return_value=[]):
            res = pv.storyboard_preview(
                "曹操",
                [{"narration": "x", "visual_query": "battle", "visual_type": "historical_photo"}],
                threshold="strict",
                use_cache=False,
            )
        self.assertEqual(res["coverage"], 0.0)
        self.assertEqual(len(res["missing_shots"]), 1)
        self.assertIn("缺素材", res["shots"][0]["warning"])

    def test_broll_shot_excluded_from_coverage(self):
        # b_roll 镜走库存源，档案预览无候选属正常，不计入缺素材警告。
        with mock.patch.object(pv.archive, "find_person_images", return_value=[]):
            res = pv.storyboard_preview(
                "曹操",
                [{"narration": "战场", "visual_query": "battle", "visual_type": "b_roll"}],
                threshold="standard",
                use_cache=False,
            )
        shot = res["shots"][0]
        self.assertEqual(shot["warning"], "")
        self.assertEqual(res["archive_shot_count"], 0)
        # b_roll 无档案候选 → 覆盖率按 0 但不应进 missing_shots 的档案缺素材列表
        self.assertEqual(res["missing_shots"], [])

    def test_cache_hit_returns_stale_flag(self):
        good = _item("wikimedia", "File:Cao_Cao.jpg", "https://x/a.jpg")
        with mock.patch.object(
            pv.archive, "find_person_images", return_value=[good]
        ) as find:
            first = pv.storyboard_preview(
                "曹操",
                [{"narration": "x", "visual_query": "portrait", "visual_type": "historical_photo"}],
                threshold="loose",
                use_cache=True,
            )
            second = pv.storyboard_preview(
                "曹操",
                [{"narration": "x", "visual_query": "portrait", "visual_type": "historical_photo"}],
                threshold="loose",
                use_cache=True,
            )
        self.assertFalse(first["cache"]["hit"])
        self.assertTrue(second["cache"]["hit"])
        # 第二次命中缓存，检索只应发生一次。
        self.assertEqual(find.call_count, 1)


class TestTruncateAudition(unittest.TestCase):
    def test_short_text_unchanged(self):
        self.assertEqual(pv.truncate_for_audition("很短。"), "很短。")

    def test_empty_returns_empty(self):
        self.assertEqual(pv.truncate_for_audition("   "), "")

    def test_long_text_truncated_near_limit(self):
        text = "曹操字孟德小名阿瞒。" * 20  # 长文本
        out = pv.truncate_for_audition(text, voice_rate=1.0, max_seconds=15)
        # 15 秒 * 4.5 字/秒 ≈ 67 字，允许句子边界带来的少量超出。
        self.assertLess(len(out), len(text))
        self.assertLessEqual(len(out), 67 + 20)

    def test_rate_scales_length(self):
        text = "abcdefghij" * 30
        slow = pv.truncate_for_audition(text, voice_rate=0.5, max_seconds=15)
        fast = pv.truncate_for_audition(text, voice_rate=2.0, max_seconds=15)
        self.assertLess(len(slow), len(fast))


if __name__ == "__main__":
    unittest.main()
