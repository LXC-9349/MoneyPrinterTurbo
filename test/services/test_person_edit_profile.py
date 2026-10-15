"""编辑资料弹窗（里程碑 2 收尾 #5）单测：标签读写 + 别名增删。

* person_store：tags 写入清洗（去空白/去重/40 字截断/上限 20）与读取往返。
* biography：PATCH tags 透传。
* person_repo：list_aliases 过滤主名行、delete_alias 主名不可删。
* workbench：别名 POST（幂等、未物化建档）/DELETE（404）端点，detail 附带 aliases。
"""

from __future__ import annotations

import gc
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.controllers.v1 import biography, workbench
from app.models.exception import HttpException
from app.models.schema import PersonAliasRequest, PersonPatchRequest
from app.services import person_repo, person_store


def _req():
    return SimpleNamespace(headers={"x-task-id": "req-1"})


class _DbCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ,
            {
                "MPT_MAIN_DB": os.path.join(self._td.name, "mpt.sqlite"),
                "MPT_ARTICLES_DIR": os.path.join(self._td.name, "articles"),
                "MPT_PERSONAL_DB": os.path.join(self._td.name, "personal.sqlite"),
                "MPT_LIBRARY_DB": os.path.join(self._td.name, "missing.sqlite"),
            },
        )
        self._env.start()
        conn = person_repo.connect()
        conn.close()
        self._patch = patch.object(workbench, "_ensure_projects_migrated", lambda: None)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._env.stop()
        # person_store 的连接随 GC 才释放；Windows 下必须先回收再删临时目录。
        gc.collect()
        self._td.cleanup()


class TestTagsStore(_DbCase):
    def test_tags_roundtrip_and_cleaning(self):
        self.assertTrue(person_store.create_custom_person("测试人物", dynasty="当代"))
        long_tag = "标" * 60
        self.assertTrue(
            person_store.update_person_meta(
                "测试人物", tags=[" 甲 ", "甲", "", long_tag] + [f"t{i}" for i in range(30)]
            )
        )
        tags = person_store.get_person("测试人物")["tags"]
        self.assertEqual(tags[0], "甲")  # 去空白且去重
        self.assertEqual(len(tags[0]), 1)
        self.assertEqual(len(tags), 20)  # 上限 20 个
        self.assertIn(long_tag[:40], tags)  # 单标签截断 40 字
        self.assertNotIn("", tags)

    def test_tags_default_empty_and_patch_replaces(self):
        person_store.create_custom_person("另一个人")
        self.assertEqual(person_store.get_person("另一个人")["tags"], [])
        person_store.update_person_meta("另一个人", tags=["A", "B"])
        self.assertEqual(person_store.get_person("另一个人")["tags"], ["A", "B"])
        # 传空列表 = 清空；不传 = 不动。
        person_store.update_person_meta("另一个人", note="只改备注")
        self.assertEqual(person_store.get_person("另一个人")["tags"], ["A", "B"])
        person_store.update_person_meta("另一个人", tags=[])
        self.assertEqual(person_store.get_person("另一个人")["tags"], [])

    def test_biography_patch_passes_tags(self):
        person_store.create_custom_person("PATCH人物")
        resp = biography.patch_person(
            _req(), PersonPatchRequest(dynasty="当代", tags=[" x ", "x", "y"]), "PATCH人物"
        )["data"]
        self.assertEqual(resp["tags"], ["x", "y"])
        self.assertEqual(resp["dynasty"], "当代")


class TestAliasRepo(_DbCase):
    def test_list_aliases_filters_main_name_row(self):
        p = person_repo.create_or_get_person("曹操")
        # create_or_get_person 会把主名也登记为别名行，编辑视图不应重复展示。
        self.assertEqual(person_repo.list_aliases("曹操"), [])
        person_repo.add_alias(p["public_id"], "阿瞒", source="manual")
        rows = person_repo.list_aliases("曹操")
        self.assertEqual([r["alias"] for r in rows], ["阿瞒"])
        self.assertEqual(rows[0]["source"], "manual")

    def test_list_aliases_unknown_person_empty(self):
        self.assertEqual(person_repo.list_aliases("查无此人"), [])

    def test_delete_alias_rules(self):
        p = person_repo.create_or_get_person("曹操")
        person_repo.add_alias(p["public_id"], "孟德", source="import")
        self.assertFalse(person_repo.delete_alias("曹操", "曹操"))  # 主名不可删
        self.assertTrue(person_repo.delete_alias("曹操", "孟德"))
        self.assertFalse(person_repo.delete_alias("曹操", "孟德"))  # 再删不存在
        self.assertFalse(person_repo.delete_alias("查无此人", "任意"))
        self.assertFalse(person_repo.delete_alias("曹操", "  "))


class TestAliasEndpoints(_DbCase):
    def test_add_endpoint_idempotent_and_materializes(self):
        resp = workbench.add_person_alias(
            _req(), "居里夫人", PersonAliasRequest(alias="玛丽·居里")
        )["data"]
        self.assertEqual([a["alias"] for a in resp["aliases"]], ["玛丽·居里"])
        # 未物化人物自动建档后可见。
        self.assertEqual(person_repo.list_aliases("居里夫人")[0]["source"], "manual")
        again = workbench.add_person_alias(
            _req(), "居里夫人", PersonAliasRequest(alias="玛丽·居里")
        )["data"]
        self.assertEqual(len(again["aliases"]), 1)  # 幂等不重复

    def test_add_endpoint_rejects_blank_and_self(self):
        for bad in ("   ", "曹操"):
            with self.assertRaises(HttpException) as raised:
                workbench.add_person_alias(_req(), "曹操", PersonAliasRequest(alias=bad))
            self.assertEqual(raised.exception.status_code, 400)

    def test_delete_endpoint_and_404(self):
        workbench.add_person_alias(_req(), "曹操", PersonAliasRequest(alias="阿瞒"))
        out = workbench.delete_person_alias(_req(), "曹操", "阿瞒")["data"]
        self.assertEqual(out["aliases"], [])
        with self.assertRaises(HttpException) as raised:
            workbench.delete_person_alias(_req(), "曹操", "阿瞒")
        self.assertEqual(raised.exception.status_code, 404)

    def test_detail_carries_aliases(self):
        profile = {"person": "李世民", "dynasty": "隋唐", "key_facts": [], "docs": []}
        workbench.add_person_alias(_req(), "李世民", PersonAliasRequest(alias="天策上将"))
        with patch.object(workbench.person_store, "get_person", return_value=profile):
            data = workbench.person_detail(_req(), "李世民")["data"]
        self.assertEqual([a["alias"] for a in data["aliases"]], ["天策上将"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
