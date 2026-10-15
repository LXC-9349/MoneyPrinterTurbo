"""人物图片素材操作（里程碑 2 收尾 #3）单测。

覆盖三层：
* person_repo：封面唯一性、排除写 state='excluded' 并取消封面、非法状态、未关联。
* asset_registry.register_uploaded_image：上传登记（自动建档、哈希去重、owned 许可）。
* workbench 端点：cover/PATCH/upload 直调 + person_detail 的 no_authentic_photo 判定。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.controllers.v1 import workbench
from app.models.exception import HttpException
from app.models.schema import PersonAssetPatchRequest
from app.services import asset_registry, person_repo
from app.utils import utils


def _req():
    return SimpleNamespace(headers={"x-task-id": "req-1"})


def _write_png(dirpath: str, name: str, data: bytes) -> str:
    p = os.path.join(dirpath, name)
    with open(p, "wb") as f:
        f.write(data)
    return p


class _RepoCase(unittest.TestCase):
    """临时库 + 已物化人物 + 两个 image 资产（101/102 由 helper 返回）。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._db = os.path.join(self._td.name, "mpt.sqlite")
        self._env = patch.dict(os.environ, {"MPT_MAIN_DB": self._db})
        self._env.start()
        self.conn = person_repo.connect()
        self.person = person_repo.create_or_get_person("曹操", conn=self.conn)
        self.asset_a = self._mk_asset("a.jpg", b"img-a")
        self.asset_b = self._mk_asset("b.jpg", b"img-b")

    def tearDown(self):
        self.conn.close()
        self._env.stop()
        self._td.cleanup()

    def _mk_asset(self, name: str, payload: bytes) -> int:
        path = _write_png(self._td.name, name, payload)
        asset_id = person_repo.upsert_asset(
            self.conn,
            kind="image",
            path=name,
            content_hash=asset_registry._sha256(path),
            provider="wikimedia",
            license="CC0",
        )
        person_repo.link_person_asset(self.conn, self.person["public_id"], asset_id)
        return asset_id

    def _covers(self) -> list[int]:
        rows = self.conn.execute(
            "SELECT asset_id FROM person_assets WHERE person_id=? AND is_cover=1",
            (self.person["person_id"],),
        ).fetchall()
        return sorted(int(r["asset_id"]) for r in rows)

    def _state(self, asset_id: int) -> tuple[str, int]:
        row = self.conn.execute(
            "SELECT state, is_cover FROM person_assets WHERE person_id=? AND asset_id=?",
            (self.person["person_id"], asset_id),
        ).fetchone()
        return row["state"], int(row["is_cover"])


class TestPersonRepoCoverAndState(_RepoCase):
    def test_cover_is_unique_per_person(self):
        self.assertTrue(person_repo.set_person_asset_cover("曹操", self.asset_a, conn=self.conn))
        self.assertEqual(self._covers(), [self.asset_a])
        # 换封面：旧封面清零，每人至多一张。
        self.assertTrue(person_repo.set_person_asset_cover("曹操", self.asset_b, conn=self.conn))
        self.assertEqual(self._covers(), [self.asset_b])

    def test_cover_on_unlinked_asset_returns_false(self):
        self.assertFalse(person_repo.set_person_asset_cover("秦始皇", 99999, conn=self.conn))

    def test_exclude_writes_state_and_clears_cover(self):
        person_repo.set_person_asset_cover("曹操", self.asset_a, conn=self.conn)
        self.assertTrue(
            person_repo.set_person_asset_state("曹操", self.asset_a, "excluded", conn=self.conn)
        )
        state, is_cover = self._state(self.asset_a)
        self.assertEqual(state, "excluded")
        self.assertEqual(is_cover, 0)  # 不残留“被排除的图仍是封面”的矛盾态

    def test_restore_to_pending(self):
        person_repo.set_person_asset_state("曹操", self.asset_b, "excluded", conn=self.conn)
        person_repo.set_person_asset_state("曹操", self.asset_b, "pending", conn=self.conn)
        self.assertEqual(self._state(self.asset_b)[0], "pending")

    def test_invalid_state_raises(self):
        with self.assertRaises(ValueError):
            person_repo.set_person_asset_state("曹操", self.asset_a, "deleted", conn=self.conn)

    def test_state_on_unlinked_asset_returns_false(self):
        self.assertFalse(
            person_repo.set_person_asset_state("曹操", 99999, "excluded", conn=self.conn)
        )

    def test_exclude_is_person_scoped(self):
        # 同一资产关联到第二个人物：排除曹操侧不影响刘备侧（assets.state 不动）。
        other = person_repo.create_or_get_person("刘备", conn=self.conn)
        person_repo.link_person_asset(self.conn, other["public_id"], self.asset_a)
        person_repo.set_person_asset_state("曹操", self.asset_a, "excluded", conn=self.conn)
        row = self.conn.execute(
            "SELECT pa.state, a.state AS global_state FROM person_assets pa "
            "JOIN assets a ON a.asset_id=pa.asset_id "
            "WHERE pa.person_id=? AND pa.asset_id=?",
            (other["person_id"], self.asset_a),
        ).fetchone()
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["global_state"], "active")


class TestRegisterUploadedImage(_RepoCase):
    def test_registers_and_links(self):
        path = _write_png(self._td.name, "upload.png", b"upload-bytes")
        asset = asset_registry.register_uploaded_image("曹操", path, source_title="自传画像.png")
        self.assertEqual(asset["kind"], "image")
        self.assertEqual(asset["provider"], "upload")
        self.assertEqual(asset["license"], "owned")
        rows = person_repo.list_person_assets("曹操", conn=self.conn)
        match = [r for r in rows if r["asset_id"] == asset["asset_id"]]
        self.assertEqual(len(match), 1)
        self.assertEqual(match[0]["state"], "pending")
        self.assertEqual(match[0]["source_title"], "自传画像.png")

    def test_creates_missing_person(self):
        path = _write_png(self._td.name, "x.png", b"new-person")
        asset_registry.register_uploaded_image("关外人物", path)
        self.assertEqual(len(person_repo.list_person_assets("关外人物", conn=self.conn)), 1)

    def test_same_content_reuses_asset_row(self):
        p1 = _write_png(self._td.name, "u1.png", b"dup-bytes")
        p2 = _write_png(self._td.name, "u2.png", b"dup-bytes")
        a1 = asset_registry.register_uploaded_image("曹操", p1)
        a2 = asset_registry.register_uploaded_image("曹操", p2)
        self.assertEqual(a1["asset_id"], a2["asset_id"])  # 内容哈希去重
        total = self.conn.execute("SELECT COUNT(*) AS n FROM assets").fetchone()["n"]
        self.assertEqual(total, 3)  # a/b 两张既有 + 本次内容一张，不因改名翻倍

    def test_empty_name_raises(self):
        with self.assertRaises(ValueError):
            asset_registry.register_uploaded_image("", "/tmp/whatever.png")


class _EndpointCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ,
            {
                "MPT_MAIN_DB": os.path.join(self._td.name, "mpt.sqlite"),
                "MPT_ARTICLES_DIR": os.path.join(self._td.name, "articles"),
            },
        )
        self._env.start()
        c = person_repo.connect()
        c.close()
        self._patch = patch.object(workbench, "_ensure_projects_migrated", lambda: None)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._env.stop()
        self._td.cleanup()

    def _seed(self) -> dict:
        conn = person_repo.connect()
        try:
            person = person_repo.create_or_get_person("曹操", dynasty="魏晋南北朝", conn=conn)
            path = _write_png(self._td.name, "seed.jpg", b"seed-bytes")
            asset_id = person_repo.upsert_asset(
                conn, kind="image", path=os.path.basename(path),
                content_hash=asset_registry._sha256(path),
            )
            person_repo.link_person_asset(conn, person["public_id"], asset_id)
            return {"public_id": person["public_id"], "asset_id": asset_id}
        finally:
            conn.close()

    def _state(self, asset_id: int) -> tuple[str, int]:
        conn = person_repo.connect()
        try:
            row = conn.execute(
                "SELECT pa.state, pa.is_cover FROM person_assets pa "
                "JOIN persons p ON p.person_id=pa.person_id "
                "WHERE p.display_name='曹操' AND pa.asset_id=?",
                (asset_id,),
            ).fetchone()
            return row["state"], int(row["is_cover"])
        finally:
            conn.close()


class TestAssetEndpoints(_EndpointCase):
    def test_cover_endpoint_ok_and_404(self):
        seeded = self._seed()
        resp = workbench.set_person_asset_cover(_req(), "曹操", seeded["asset_id"])
        self.assertTrue(resp["data"]["is_cover"])
        with self.assertRaises(HttpException) as raised:
            workbench.set_person_asset_cover(_req(), "曹操", 99999)
        self.assertEqual(raised.exception.status_code, 404)

    def test_patch_endpoint_exclude_restore_and_400(self):
        seeded = self._seed()
        workbench.set_person_asset_cover(_req(), "曹操", seeded["asset_id"])
        workbench.patch_person_asset(
            _req(), "曹操", seeded["asset_id"], PersonAssetPatchRequest(state="excluded")
        )
        state, is_cover = self._state(seeded["asset_id"])
        self.assertEqual((state, is_cover), ("excluded", 0))
        workbench.patch_person_asset(
            _req(), "曹操", seeded["asset_id"], PersonAssetPatchRequest(state="pending")
        )
        self.assertEqual(self._state(seeded["asset_id"])[0], "pending")
        with self.assertRaises(HttpException) as raised:
            workbench.patch_person_asset(
                _req(), "曹操", seeded["asset_id"], PersonAssetPatchRequest(state="deleted")
            )
        self.assertEqual(raised.exception.status_code, 400)

    def test_upload_endpoint_registers_image(self):
        class FakeUpload:
            filename = "照片.PNG"

            async def read(self):
                return b"\x89PNG fake payload"

        target_root = os.path.join(self._td.name, "tasks")
        with patch.object(utils, "task_dir", return_value=target_root):
            data = asyncio.run(
                workbench.upload_person_image(_req(), "海明威", FakeUpload())
            )["data"]
        self.assertEqual(data["kind"], "image")
        self.assertEqual(data["license"], "owned")
        # 文件确实落在 task_dir()/person_images/ 下（path 存相对 storage 根或跨盘绝对路径，
        # 两种形态都以文件名定位）。
        stored = os.path.join(target_root, "person_images", os.path.basename(data["path"]))
        self.assertTrue(os.path.isfile(stored))
        rows = person_repo.list_person_assets("海明威")
        self.assertEqual([r["asset_id"] for r in rows], [data["asset_id"]])

    def test_upload_endpoint_rejects_non_image(self):
        class FakeUpload:
            filename = "notes.txt"

            async def read(self):
                return b"hello"

        with self.assertRaises(HttpException) as raised:
            asyncio.run(workbench.upload_person_image(_req(), "曹操", FakeUpload()))
        self.assertEqual(raised.exception.status_code, 400)

    def test_upload_endpoint_rejects_empty_file(self):
        class FakeUpload:
            filename = "a.jpg"

            async def read(self):
                return b""

        with self.assertRaises(HttpException) as raised:
            asyncio.run(workbench.upload_person_image(_req(), "曹操", FakeUpload()))
        self.assertEqual(raised.exception.status_code, 400)


class TestNoAuthenticPhoto(_EndpointCase):
    def _detail_flag(self, dynasty: str) -> bool:
        profile = {"person": "某人", "dynasty": dynasty, "key_facts": [], "docs": []}
        with patch.object(workbench.person_store, "get_person", return_value=profile):
            return workbench.person_detail(_req(), "某人")["data"]["no_authentic_photo"]

    def test_ancient_dynasty_true(self):
        self.assertTrue(self._detail_flag("魏晋南北朝"))
        self.assertTrue(self._detail_flag("清"))

    def test_modern_or_foreign_false(self):
        self.assertFalse(self._detail_flag("民国"))
        self.assertFalse(self._detail_flag("当代"))
        self.assertFalse(self._detail_flag("外国"))
        self.assertFalse(self._detail_flag(""))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
