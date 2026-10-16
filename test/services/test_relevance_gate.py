"""M4 相关性门槛与坏例回归测试。

验证：
1. "tambor de mina"（LoC 返回的巴西鼓乐文档）被正确判定为低相关
2. 强匹配/中匹配/弱关联/噪声各档位行为正确
3. LoC 裸子串匹配已关闭
4. 三档阈值过滤效果
"""
import unittest
from app.models.schema import MaterialInfo
from app.services.relevance_gate import (
    THRESHOLDS,
    filter_candidates,
    score_relevance,
)


def _make_item(provider: str, asset_id: str, source_page: str = "", search_term: str = "") -> MaterialInfo:
    item = MaterialInfo()
    item.provider = provider
    item.url = f"https://example.com/{asset_id[:20]}"
    item.duration = 0
    item.source_info = {
        "provider": provider,
        "asset_id": asset_id,
        "source_page": source_page,
        "search_term": search_term,
        "credit": "",
        "license": "public domain",
        "creator": {"name": ""},
    }
    return item


class TestRelevanceScoring(unittest.TestCase):
    """基础评分逻辑验证。"""

    PERSON = "曹操"
    ALIASES = ["Cao Cao", "孟德"]
    ERA = ["Three Kingdoms", "Han Dynasty"]

    def test_strong_match_asset_id(self):
        """asset_id 包含人名 → 0.95。"""
        item = _make_item("wikimedia", "File:Cao_Cao_portrait.jpg")
        score, reason = score_relevance(
            item, person_name=self.PERSON, aliases=self.ALIASES, era_keywords=self.ERA
        )
        self.assertGreaterEqual(score, 0.9)
        self.assertIn("asset_id", reason)

    def test_medium_match_source_page(self):
        """source_page 包含人名 → 0.7。"""
        item = _make_item(
            "wikimedia", "File:ancient_painting.jpg",
            source_page="https://commons.wikimedia.org/wiki/Category:Cao_Cao",
        )
        score, _ = score_relevance(
            item, person_name=self.PERSON, aliases=self.ALIASES, era_keywords=self.ERA
        )
        self.assertGreaterEqual(score, 0.7)

    def test_era_keyword_match(self):
        """时代关键词命中 → 0.5。"""
        item = _make_item(
            "wikimedia", "File:Three_Kingdoms_map.jpg",
        )
        score, reason = score_relevance(
            item, person_name=self.PERSON, aliases=self.ALIASES, era_keywords=self.ERA
        )
        self.assertEqual(score, 0.5)
        self.assertIn("时代关键词", reason)

    def test_weak_search_term_only(self):
        """仅搜索词含人名，结果本身无关 → 0.3。"""
        item = _make_item(
            "wikimedia", "File:unrelated_landscape.jpg",
            search_term="Cao Cao",
        )
        score, _ = score_relevance(
            item, person_name=self.PERSON, aliases=self.ALIASES, era_keywords=self.ERA
        )
        self.assertEqual(score, 0.3)

    def test_noise_no_match(self):
        """完全不相关 → 0.1。"""
        item = _make_item("wikimedia", "File:Paris_Eiffel_Tower.jpg")
        score, _ = score_relevance(
            item, person_name=self.PERSON, aliases=self.ALIASES, era_keywords=self.ERA
        )
        self.assertEqual(score, 0.1)


class TestLocBareSubstringDisabled(unittest.TestCase):
    """LoC 关闭裸子串匹配：source_page/credit 中的命中不生效。"""

    def test_loc_source_page_match_ignored(self):
        """LoC 结果的 source_page 包含人名不应得分。"""
        item = _make_item(
            "loc", "File:brazilian_music_1940.jpg",
            source_page="https://www.loc.gov/item/caocao-collection/",
        )
        score, _ = score_relevance(
            item, person_name="曹操", aliases=["Cao Cao"], era_keywords=[],
            provider="loc",
        )
        # source_page 不应被 LoC 检查（已关闭）；asset_id 也不含人名
        self.assertLess(score, 0.7)

    def test_loc_asset_id_still_works(self):
        """LoC 的 asset_id 包含人名仍然有效（强匹配）。"""
        item = _make_item("loc", "Einstein_portrait_1921.jpg")
        score, _ = score_relevance(
            item, person_name="爱因斯坦", aliases=["Albert Einstein", "Einstein"], era_keywords=[],
            provider="loc",
        )
        self.assertGreaterEqual(score, 0.9)


class TestTamborDeMinaRegression(unittest.TestCase):
    """已知坏例回归：搜索中国历史人物时 LoC 返回巴西鼓乐文档。

    这是里程碑 4 启动的直接动因——该噪声结果必须被门槛拦截。
    """

    def test_tambor_de_mina_discarded_for_caocao(self):
        """LoC "tambor de mina" 对曹操完全无关，应在 standard 档被丢弃。"""
        bad_item = _make_item(
            "loc",
            "tambor_de_mina_brazilian_drum_music_documentation.jpg",
            source_page="https://www.loc.gov/item/tambor-de-mina/",
        )
        score, _ = score_relevance(
            bad_item,
            person_name="曹操",
            aliases=["Cao Cao", "孟德", "Emperor Wu of Wei"],
            era_keywords=["Three Kingdoms", "Han Dynasty", "Chinese general"],
            provider="loc",
        )
        # 标准档 0.4；噪声 0.1 远低于门槛
        self.assertLess(score, THRESHOLDS["standard"])

        # 通过 filter_candidates 确认被丢弃
        passed, discarded = filter_candidates(
            [bad_item],
            person_name="曹操",
            aliases=["Cao Cao", "孟德"],
            era_keywords=["Three Kingdoms"],
            threshold="standard",
        )
        self.assertEqual(len(passed), 0)
        self.assertEqual(len(discarded), 1)
        self.assertIn("tambor", discarded[0]["title"].lower())

    def test_tambor_de_mina_also_discarded_in_loose(self):
        """即便宽松档 (0.2)，0.1 分噪声仍然被过滤。"""
        bad_item = _make_item(
            "loc",
            "tambor_de_mina_brazilian_drum_music_documentation.jpg",
        )
        passed, discarded = filter_candidates(
            [bad_item],
            person_name="李世民",
            aliases=["Li Shimin", "唐太宗"],
            era_keywords=["Tang Dynasty"],
            threshold="loose",
        )
        self.assertEqual(len(passed), 0)


class TestThreeThresholds(unittest.TestCase):
    """三档阈值行为验证。"""

    def _mixed_items(self):
        strong = _make_item("wikimedia", "File:Albert_Einstein_Nobel.jpg")
        era_match = _make_item("wikimedia", "File:20th_century_physics.jpg")
        weak = _make_item("wikimedia", "File:random_lab.jpg", search_term="Einstein")
        noise = _make_item("wikimedia", "File:Brazilian_carnival.jpg")
        return [strong, era_match, weak, noise]

    def test_strict_only_keeps_strong(self):
        passed, _ = filter_candidates(
            self._mixed_items(),
            person_name="爱因斯坦",
            aliases=["Albert Einstein", "Einstein"],
            era_keywords=["physicist", "20th century"],
            threshold="strict",
        )
        # strict=0.7: strong(0.95) passes; era_match(0.5) fails; weak(0.3) fails
        self.assertEqual(len(passed), 1)

    def test_standard_keeps_strong_and_era(self):
        passed, _ = filter_candidates(
            self._mixed_items(),
            person_name="爱因斯坦",
            aliases=["Albert Einstein", "Einstein"],
            era_keywords=["physicist", "20th century"],
            threshold="standard",
        )
        # standard=0.4: strong(0.95) + era(0.5) → 2 pass
        self.assertEqual(len(passed), 2)

    def test_loose_keeps_all_but_noise(self):
        passed, _ = filter_candidates(
            self._mixed_items(),
            person_name="爱因斯坦",
            aliases=["Albert Einstein", "Einstein"],
            era_keywords=["physicist", "20th century"],
            threshold="loose",
        )
        # loose=0.2: strong(0.95) + era(0.5) + weak(0.3) → 3 pass, noise(0.1) out
        self.assertEqual(len(passed), 3)


if __name__ == "__main__":
    unittest.main()
