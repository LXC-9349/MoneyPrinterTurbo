"""M4#3 逐镜独立检索 / 近重复去重 / 人物级黑名单强制 / 缺素材不静默。

全合成，无网络。核心验证 historical_archive.find_person_images 与
person_repo.excluded_asset_keys，以及缺素材提醒落盘。
"""
import os
import tempfile
import unittest
from unittest import mock

from app.models.schema import MaterialInfo
from app.services import person_repo
from app.services.archives import historical_archive as h


def _item(provider, asset_id, url, source_page=None):
    it = MaterialInfo()
    it.provider = provider
    it.url = url
    it.duration = 0
    it.source_info = {
        "provider": provider,
        "asset_id": asset_id,
        "source_page": source_page or f"https://commons/{asset_id}",
        "credit": "",
        "search_term": "",
        "license": "public domain",
        "creator": {"name": ""},
    }
    return it


class TestNearDuplicateAndBlacklist(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(
            h, "person_gate_context",
            return_value={
                "name": "曹操",
                "aliases": ["Cao Cao"],
                "era_keywords": ["Three Kingdoms"],
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_near_duplicate_collapses_same_asset_id(self):
        # 同一张照片的两种尺寸/文件名命中：asset_id 归一后身份相同。
        a = _item("wikimedia", "File:Cao_Cao_portrait.jpg", "https://x/big.jpg")
        b = _item("wikimedia", "File:Cao_Cao_portrait.jpg", "https://x/small.jpg")
        with mock.patch.object(h, "search_wikimedia_images", return_value=[a, b]), \
             mock.patch.object(h, "search_loc_images", return_value=[]):
            out = h.find_person_images("曹操", limit=6, threshold="off")
        self.assertEqual(len(out), 1)

    def test_excluded_asset_skipped(self):
        good = _item("wikimedia", "File:Cao_Cao_wei.jpg", "https://x/g.jpg")
        bad = _item("wikimedia", "File:Cao_Cao_bad.jpg", "https://x/b.jpg")
        excluded = {"https://commons/file:cao_cao_bad.jpg"}
        with mock.patch.object(h, "search_wikimedia_images", return_value=[good, bad]), \
             mock.patch.object(h, "search_loc_images", return_value=[]):
            out = h.find_person_images(
                "曹操", limit=6, threshold="off", excluded_keys=excluded
            )
        urls = {i.url for i in out}
        self.assertIn("https://x/g.jpg", urls)
        self.assertNotIn("https://x/b.jpg", urls)


class TestExcludedAssetKeys(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._db = os.path.join(self._td.name, "mpt.sqlite")
        self._env = mock.patch.dict(os.environ, {"MPT_MAIN_DB": self._db})
        self._env.start()
        conn = person_repo.connect()
        conn.close()

    def tearDown(self):
        self._env.stop()
        self._td.cleanup()

    def test_returns_only_excluded_source_pages(self):
        person = person_repo.create_or_get_person("曹操")
        c = person_repo.connect()
        try:
            aid_keep = person_repo.upsert_asset(
                c, kind="image", path="a.jpg", content_hash="h1", size_bytes=1,
                provider="wikimedia", license="PD", source_page="https://commons/keep",
            )
            aid_drop = person_repo.upsert_asset(
                c, kind="image", path="b.jpg", content_hash="h2", size_bytes=1,
                provider="wikimedia", license="PD", source_page="https://commons/drop",
            )
            person_repo.link_person_asset(c, person["public_id"], aid_keep, state="approved")
            person_repo.link_person_asset(c, person["public_id"], aid_drop, state="pending")
        finally:
            c.close()
        # 排除 b
        person_repo.set_person_asset_state("曹操", aid_drop, "excluded")
        keys = person_repo.excluded_asset_keys("曹操")
        self.assertIn("https://commons/drop", keys)
        self.assertNotIn("https://commons/keep", keys)

    def test_unmaterialized_person_returns_empty(self):
        self.assertEqual(person_repo.excluded_asset_keys("不存在的人"), set())


class TestShortageReminderPersist(unittest.TestCase):
    def test_writes_shortage_and_relevance(self):
        missing = [{"shot_index": 3, "query": "Battle of Guandu", "reason": "独立相关素材不足"}]
        discarded = [{"title": "noise", "url": "https://x/n.jpg", "score": 0.1, "reason": "无"}]
        with mock.patch("app.services.task_artifacts.patch_script_data") as ps:
            h._persist_relevance_reminders("t9", "曹操", discarded, missing_shots=missing)
        _, kwargs = ps.call_args
        self.assertIn("material_relevance_reminders", kwargs)
        self.assertIn("material_shortage_reminders", kwargs)
        self.assertEqual(kwargs["material_shortage_reminders"]["missing_count"], 1)
        self.assertIn("b-roll", kwargs["material_shortage_reminders"]["note"])

    def test_shortage_only_still_persists(self):
        missing = [{"shot_index": 0, "query": "q", "reason": "r"}]
        with mock.patch("app.services.task_artifacts.patch_script_data") as ps:
            h._persist_relevance_reminders("t10", "曹操", [], missing_shots=missing)
        self.assertTrue(ps.called)


if __name__ == "__main__":
    unittest.main()
