"""素材资产登记（里程碑 2 收尾 #2）单测。

隔离到临时库（MPT_MAIN_DB）与临时素材目录，验证：图片按 image_file 优先登记、
内容哈希去重、来源字段映射、人物关联、缺文件跳过、无 image_file 时退回片段、
空人物名不建任何行。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from app.services import asset_registry, person_repo


def _write_file(dirpath: str, name: str, data: bytes) -> str:
    p = os.path.join(dirpath, name)
    with open(p, "wb") as f:
        f.write(data)
    return p


class AssetRegistryTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._db = os.path.join(self._td.name, "mpt.sqlite")
        self._mat = os.path.join(self._td.name, "task")
        os.makedirs(self._mat, exist_ok=True)
        self._env = patch.dict(os.environ, {"MPT_MAIN_DB": self._db})
        self._env.start()
        conn = person_repo.connect()
        conn.close()

    def tearDown(self):
        self._env.stop()
        self._td.cleanup()

    def _sources(self):
        c = person_repo.connect()
        try:
            rows = c.execute(
                """SELECT a.kind, a.path, a.license, a.author, a.source_page,
                          a.source_title, pa.state
                   FROM assets a JOIN person_assets pa ON pa.asset_id=a.asset_id
                   JOIN persons p ON p.person_id=pa.person_id
                   WHERE p.display_name=?""",
                ("曹操",),
            ).fetchall()
            count = c.execute("SELECT COUNT(*) AS n FROM assets").fetchone()["n"]
            return [dict(r) for r in rows], int(count)
        finally:
            c.close()

    def test_registers_image_from_image_file_with_provenance(self):
        img = _write_file(self._mat, "photo.jpg", b"\xff\xd8\xffcaocaopic")
        clip = _write_file(self._mat, "clip.mp4", b"notanimage")
        sources = [
            {
                "provider": "wikimedia",
                "local_file": os.path.basename(clip),
                "image_file": os.path.basename(img),
                "license": "CC0",
                "license_url": "https://x/cc0",
                "creator": {"name": "Alice"},
                "source_page": "https://commons/photo",
                "search_term": "曹操像",
            }
        ]
        out = asset_registry.register_material_assets(
            "t1",
            person_name="曹操",
            material_paths=[clip],
            material_sources=sources,
        )
        self.assertEqual(out["registered"], 1)
        self.assertEqual(out["linked"], 1)
        rows, total = self._sources()
        self.assertEqual(total, 1)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        # 登记的是真实照片（image_file），不是渲染片段。
        self.assertEqual(r["kind"], "image")
        self.assertTrue(r["path"].endswith("photo.jpg"))
        self.assertEqual(r["license"], "CC0")
        self.assertEqual(r["author"], "Alice")
        self.assertEqual(r["source_page"], "https://commons/photo")
        self.assertEqual(r["source_title"], "曹操像")
        self.assertEqual(r["state"], "pending")

    def test_dedup_by_content_hash(self):
        # 两条记录指向内容完全相同的图片（不同文件名）→ 只登记一个 asset。
        a = _write_file(self._mat, "a.jpg", b"same-bytes")
        b = _write_file(self._mat, "b.jpg", b"same-bytes")
        sources = [
            {"local_file": "a.jpg", "image_file": "a.jpg"},
            {"local_file": "b.jpg", "image_file": "b.jpg"},
        ]
        out = asset_registry.register_material_assets(
            "t2", person_name="曹操", material_paths=[a, b], material_sources=sources
        )
        # 内容哈希相同 → 第二个 seen 命中被跳过，只登记 1 次。
        self.assertEqual(out["registered"], 1)
        _, total = self._sources()
        self.assertEqual(total, 1)

    def test_repeated_registration_is_idempotent(self):
        img = _write_file(self._mat, "p.jpg", b"payload-1")
        sources = [{"local_file": "p.jpg", "image_file": "p.jpg", "license": "PD"}]
        asset_registry.register_material_assets(
            "t3", person_name="曹操", material_paths=[img], material_sources=sources
        )
        # 第二次跑同一素材：内容哈希去重（assets 复用），人物关联不新增。
        second = asset_registry.register_material_assets(
            "t3", person_name="曹操", material_paths=[img], material_sources=sources
        )
        self.assertEqual(second["linked"], 0)
        _, total = self._sources()
        self.assertEqual(total, 1)

    def test_video_clip_fallback_when_no_image_file(self):
        clip = _write_file(self._mat, "stock.mp4", b"videobytes")
        sources = [{"provider": "pexels", "local_file": "stock.mp4"}]
        out = asset_registry.register_material_assets(
            "t4", person_name="曹操", material_paths=[clip], material_sources=sources
        )
        self.assertEqual(out["registered"], 1)
        rows, _ = self._sources()
        self.assertEqual(rows[0]["kind"], "video")

    def test_skips_missing_files(self):
        sources = [{"local_file": "gone.jpg", "image_file": "gone.jpg"}]
        present = _write_file(self._mat, "x.jpg", b"present")
        out = asset_registry.register_material_assets(
            "t5", person_name="曹操", material_paths=[present], material_sources=sources
        )
        self.assertEqual(out["registered"], 0)
        self.assertEqual(out["skipped"], 1)

    def test_no_person_name_is_noop(self):
        img = _write_file(self._mat, "n.jpg", b"data")
        out = asset_registry.register_material_assets(
            "t6",
            person_name="",
            material_paths=[img],
            material_sources=[{"local_file": "n.jpg"}],
        )
        self.assertEqual(out, {"registered": 0, "linked": 0, "skipped": 0})
        _, total = self._sources()
        self.assertEqual(total, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
