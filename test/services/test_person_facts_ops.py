"""结构化事实操作（里程碑 2 收尾 #4）单测。

* person_repo：list/update/delete + key_facts 惰性迁入（幂等、未核验态）。
* workbench：facts 新增/PATCH（采纳/编辑）/DELETE 端点，person_detail 返回
  结构化 facts 且 fact_count 与表行数一致。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.controllers.v1 import workbench
from app.models.exception import HttpException
from app.models.schema import FactCreateRequest, FactPatchRequest
from app.services import person_repo


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
            },
        )
        self._env.start()
        self.conn = person_repo.connect()
        self.conn.close()
        self._patch = patch.object(workbench, "_ensure_projects_migrated", lambda: None)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._env.stop()
        self._td.cleanup()


class TestFactRepo(_DbCase):
    def _person(self, name="曹操"):
        return person_repo.create_or_get_person(name)

    def test_import_overlay_facts_marks_unverified(self):
        n = person_repo.import_overlay_facts("李世民", ["事实一", " 事实二 ", "", "事实三"])
        self.assertEqual(n, 3)  # 空白条目被过滤
        facts = person_repo.list_facts("李世民")
        self.assertEqual([f["statement"] for f in facts], ["事实一", "事实二", "事实三"])
        self.assertEqual([f["ordinal"] for f in facts], [0, 1, 2])
        self.assertTrue(all(f["source"] == "overlay" for f in facts))
        self.assertTrue(all(f["verified"] == 0 for f in facts))  # 迁入默认待核验
        # 幂等：已有行再导不追加。
        self.assertEqual(person_repo.import_overlay_facts("李世民", ["再来一条"]), 0)
        self.assertEqual(len(person_repo.list_facts("李世民")), 3)

    def test_import_empty_is_noop(self):
        self.assertEqual(person_repo.import_overlay_facts(" Einstein", []), 0)
        self.assertEqual(person_repo.list_facts("爱因斯坦"), [])

    def test_list_facts_unknown_person_empty(self):
        self.assertEqual(person_repo.list_facts("查无此人"), [])

    def test_update_fact_statement_marks_edited(self):
        p = self._person()
        fid = person_repo.add_fact(p["public_id"], "官渡之战大胜", source="llm", verified=0)
        row = person_repo.update_fact(fid, statement="官渡之战以少胜多", human_confirmed=1)
        self.assertEqual(row["statement"], "官渡之战以少胜多")
        self.assertEqual(row["edited_by_user"], 1)
        self.assertEqual(row["human_confirmed"], 1)
        self.assertEqual(row["verified"], 0)  # 未显式传 verified 不动核验位

    def test_update_accept_actions_and_missing(self):
        p = self._person()
        fid = person_repo.add_fact(p["public_id"], "待核验事实", verified=0)
        row = person_repo.update_fact(fid, verified=1, human_confirmed=1)  # 采纳
        self.assertEqual((row["verified"], row["human_confirmed"]), (1, 1))
        self.assertIsNone(person_repo.update_fact(999999, human_confirmed=1))

    def test_delete_fact(self):
        p = self._person()
        fid = person_repo.add_fact(p["public_id"], "将被忽略")
        self.assertTrue(person_repo.delete_fact(fid))
        self.assertFalse(person_repo.delete_fact(fid))


class TestFactEndpoints(_DbCase):
    def test_add_endpoint_materializes_and_confirms(self):
        resp = workbench.add_person_fact(
            _req(), "居里夫人", FactCreateRequest(statement="两次获诺贝尔奖", source_ref="维基")
        )["data"]
        self.assertEqual(resp["verified"], 1)
        self.assertEqual(resp["human_confirmed"], 1)
        self.assertEqual(resp["source"], "manual")
        rows = person_repo.list_facts("居里夫人")  # 未物化人物经新增自动建档
        self.assertEqual([r["statement"] for r in rows], ["两次获诺贝尔奖"])

    def test_add_endpoint_requires_statement(self):
        with self.assertRaises(HttpException) as raised:
            workbench.add_person_fact(_req(), "曹操", FactCreateRequest(statement="   "))
        self.assertEqual(raised.exception.status_code, 400)

    def test_patch_delete_endpoints_and_404(self):
        created = workbench.add_person_fact(
            _req(), "曹操", FactCreateRequest(statement="挟天子以令诸侯")
        )["data"]
        patched = workbench.patch_person_fact(
            _req(), created["fact_id"], FactPatchRequest(statement="奉天子以令不臣")
        )["data"]
        self.assertEqual(patched["statement"], "奉天子以令不臣")
        self.assertEqual(patched["edited_by_user"], 1)
        out = workbench.delete_person_fact(_req(), created["fact_id"])["data"]
        self.assertTrue(out["deleted"])
        with self.assertRaises(HttpException) as raised:
            workbench.patch_person_fact(_req(), created["fact_id"], FactPatchRequest(verified=True))
        self.assertEqual(raised.exception.status_code, 404)
        with self.assertRaises(HttpException) as raised:
            workbench.delete_person_fact(_req(), created["fact_id"])
        self.assertEqual(raised.exception.status_code, 404)

    def test_detail_lazy_imports_overlay_key_facts(self):
        profile = {
            "person": "李世民",
            "dynasty": "隋唐",
            "key_facts": ["玄武门之变", "贞观之治"],
            "docs": [],
        }
        with patch.object(workbench.person_store, "get_person", return_value=profile):
            first = workbench.person_detail(_req(), "李世民")["data"]
            second = workbench.person_detail(_req(), "李世民")["data"]
        self.assertEqual(first["summary"]["fact_count"], 2)
        self.assertEqual(
            [f["statement"] for f in first["facts"]], ["玄武门之变", "贞观之治"]
        )
        self.assertTrue(all(f["source"] == "overlay" for f in first["facts"]))
        self.assertTrue(all(f["verified"] == 0 for f in first["facts"]))
        # 二次访问不重复迁入。
        self.assertEqual(second["summary"]["fact_count"], 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
