"""M4#2 相关性门槛接入 historical_archive 的集成测试（全合成，无网络）。

验证 find_person_images：
1. 与人物名/别名/时代关键词匹配的候选保留，噪声被丢弃；
2. 被丢弃项写入 discarded_out，字段供任务提醒使用；
3. threshold="off" 时全部通过（保持既有行为）；
4. _persist_relevance_reminders 把丢弃项落进任务清单 script.json。
"""
import json
import unittest
from unittest import mock

from app.models.schema import MaterialInfo
from app.services.archives import historical_archive as h


def _item(provider: str, asset_id: str, url: str) -> MaterialInfo:
    it = MaterialInfo()
    it.provider = provider
    it.url = url
    it.duration = 0
    it.source_info = {
        "provider": provider,
        "asset_id": asset_id,
        "source_page": f"https://example.org/{asset_id}",
        "credit": "",
        "search_term": "",
        "license": "public domain",
        "creator": {"name": ""},
    }
    return it


class TestFindPersonImagesGate(unittest.TestCase):
    def setUp(self):
        # 曹操：别名含 "Cao Cao"，时代词含 "Three Kingdoms"。
        self.good = [
            _item("wikimedia", "File:Cao_Cao_portrait.jpg", "https://x/1.jpg"),
            _item("wikimedia", "File:Three_Kingdoms_map.jpg", "https://x/2.jpg"),
        ]
        self.noise = [
            _item("loc", "tambor_de_mina_brazil_drum.jpg", "https://x/3.jpg"),
            _item("wikimedia", "File:Paris_Eiffel_Tower.jpg", "https://x/4.jpg"),
        ]
        patcher_ctx = mock.patch.object(
            h, "person_gate_context",
            return_value={
                "name": "曹操",
                "aliases": ["Cao Cao", "孟德"],
                "era_keywords": ["Three Kingdoms"],
            },
        )
        patcher_ctx.start()
        self.addCleanup(patcher_ctx.stop)
        # 批次 B 新增的结构化取材（Wikidata/Commons）会联网，本测试为全合成，
        # 把它清空，只验证名称检索 + 相关性门槛的既有语义。
        patcher_struct = mock.patch.object(
            h, "structural_person_materials", return_value=[]
        )
        patcher_struct.start()
        self.addCleanup(patcher_struct.stop)

    def _mock_search(self):
        all_items = self.good + self.noise
        return mock.patch.object(
            h, "search_wikimedia_images", return_value=list(all_items)
        ), mock.patch.object(h, "search_loc_images", return_value=[])

    def test_noise_discarded_relevant_kept(self):
        p1, p2 = self._mock_search()
        with p1, p2:
            discarded: list[dict] = []
            out = h.find_person_images("曹操", limit=6, discarded_out=discarded)
        urls = {i.url for i in out}
        # 强匹配 + 时代关键词命中（0.95/0.5 ≥ standard 0.4）保留
        self.assertIn("https://x/1.jpg", urls)
        self.assertIn("https://x/2.jpg", urls)
        # 噪声被丢弃
        self.assertNotIn("https://x/3.jpg", urls)
        self.assertNotIn("https://x/4.jpg", urls)
        # 丢弃记录带来源与原因
        titles = {d["title"].lower() for d in discarded}
        self.assertTrue(any("tambor" in t for t in titles))
        self.assertTrue(all("score" in d and "reason" in d for d in discarded))

    def test_threshold_off_keeps_everything(self):
        p1, p2 = self._mock_search()
        with p1, p2:
            discarded: list[dict] = []
            out = h.find_person_images(
                "曹操", limit=10, discarded_out=discarded, threshold="off"
            )
        self.assertEqual(len(out), 4)
        self.assertEqual(discarded, [])

    def test_persist_writes_script_data(self):
        payload = {
            "provider": "loc",
            "title": "tambor_de_mina.jpg",
            "source_page": "https://loc.gov/x",
            "score": 0.1,
            "reason": "未命中任何关键词",
            "url": "https://x/3.jpg",
        }
        with mock.patch("app.services.task_artifacts.patch_script_data") as ps:
            h._persist_relevance_reminders("t-1", "曹操", [payload, payload])
        self.assertTrue(ps.called)
        _, kwargs = ps.call_args
        rec = kwargs["material_relevance_reminders"]
        self.assertEqual(rec["person"], "曹操")
        # 同一素材去重
        self.assertEqual(rec["discarded_count"], 1)

    def test_persist_noop_when_empty(self):
        with mock.patch("app.services.task_artifacts.patch_script_data") as ps:
            h._persist_relevance_reminders("t-1", "曹操", [])
        ps.assert_not_called()


if __name__ == "__main__":
    unittest.main()
