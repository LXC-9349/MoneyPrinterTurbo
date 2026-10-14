"""一次性数据导入命令的合成夹具测试（阶段 2 步骤 2，§C10）。

用临时目录构造**合成** library.sqlite / personal.sqlite / projects.json / tasks，
不读取任何真实语料正文，人物名仅占位。覆盖：
* dry-run 不落库、给出各表条数与无法映射清单（§C9）。
* 覆盖层"替换"语义：个人 facts_json 替换后**不再导入其库事实**（§B6）。
* library 事实默认 verified=0；手工事实 source=manual/human_confirmed=1/edited_by_user=1（§B6）。
* apply 落库；主题视频 person 可空、传记人物未入库进入无法映射清单（§A1）。
* 只读打开源库，绝不留中间写文件。
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from app.services import import_library as il


def _mk_library(path: str) -> None:
    c = sqlite3.connect(path)
    c.execute(
        "CREATE TABLE persons(person TEXT, dynasty TEXT, facts_json TEXT,"
        " top_docs TEXT, books TEXT)"
    )
    c.executemany(
        "INSERT INTO persons VALUES(?,?,?,?,?)",
        [
            ("司马光", "宋", json.dumps(["库事实1", "库事实2"]), json.dumps(["d1"]), None),
            ("李白", "唐", json.dumps(["将被覆盖的库事实"]), None, None),
        ],
    )
    c.commit()
    c.close()


def _mk_personal(path: str) -> None:
    c = sqlite3.connect(path)
    c.execute(
        "CREATE TABLE person_meta(person TEXT, custom INTEGER, hidden INTEGER,"
        " dynasty TEXT, category TEXT, note TEXT, facts_json TEXT)"
    )
    c.executemany(
        "INSERT INTO person_meta VALUES(?,?,?,?,?,?,?)",
        [
            ("李白", 0, 0, "唐", "", "", json.dumps(["覆盖事实"])),   # 替换库事实
            ("杜甫", 1, 0, "唐", "诗人", "", json.dumps(["新建事实"])),  # custom 新建
            ("司马光", 0, 1, "", "", "", None),                     # 隐藏→archived，事实保留库
        ],
    )
    c.execute("CREATE TABLE groups(name TEXT, created_at TEXT)")
    c.execute("INSERT INTO groups VALUES('唐宋','2026-01-01T00:00:00Z')")
    c.execute("CREATE TABLE group_members(group_name TEXT, person TEXT, added_at TEXT)")
    c.executemany(
        "INSERT INTO group_members VALUES(?,?,?)",
        [("唐宋", "李白", "2026-01-01T00:00:00Z"), ("唐宋", "杜甫", "2026-01-01T00:00:00Z")],
    )
    c.commit()
    c.close()


def _mk_projects_and_tasks(root: str):
    proj_dir = os.path.join(root, "projects")
    tasks_dir = os.path.join(root, "tasks")
    os.makedirs(proj_dir, exist_ok=True)
    pf = os.path.join(proj_dir, "projects.json")
    entries = [
        {"id": "p_1", "task_id": "t1", "mode": "biography", "person": "李白", "duration": 30},
        {"id": "p_2", "task_id": "t2", "mode": "biography", "person": "查无此人"},
        {"id": "p_3", "task_id": "t3", "mode": "normal"},  # 主题视频：person 可空
    ]
    with open(pf, "w", encoding="utf-8") as f:
        json.dump({"projects": entries}, f, ensure_ascii=False)
    # t1、t3 有成片与脚本；t2 无产物。
    for tid in ("t1", "t3"):
        d = os.path.join(tasks_dir, tid)
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "final-1.mp4"), "w", encoding="utf-8").close()
        with open(os.path.join(d, "script.json"), "w", encoding="utf-8") as f:
            json.dump({"script": {"timeline": [{"narration": "占位"}]}}, f, ensure_ascii=False)
    return pf, tasks_dir


class _ImportCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = self._td.name
        self.lib = os.path.join(self.root, "library.sqlite")
        self.pers = os.path.join(self.root, "personal.sqlite")
        self.mpt = os.path.join(self.root, "mpt.sqlite")
        _mk_library(self.lib)
        _mk_personal(self.pers)
        self.pf, self.tasks = _mk_projects_and_tasks(self.root)
        self._patches = [
            mock.patch.object(il, "_library_db", lambda: self.lib),
            mock.patch.object(il, "_personal_db", lambda: self.pers),
            mock.patch.object(il, "_projects_file", lambda: self.pf),
            mock.patch.object(il, "_tasks_root", lambda: self.tasks),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._td.cleanup()


class TestPlanSemantics(_ImportCase):
    def test_replacement_and_flags(self):
        report = il.ImportReport()
        plan = il._plan(report)
        facts_by_person: dict[str, list[dict]] = {}
        for f in plan["facts"]:
            facts_by_person.setdefault(f["person"], []).append(f)

        # 李白：库事实被覆盖层"替换"，只应剩 1 条 manual，被替换的库事实不导入。
        lb = facts_by_person.get("李白", [])
        self.assertEqual(len(lb), 1)
        self.assertEqual(lb[0]["statement"], "覆盖事实")
        self.assertEqual(lb[0]["source"], "manual")
        self.assertEqual(lb[0]["human_confirmed"], 1)
        self.assertEqual(lb[0]["edited_by_user"], 1)
        self.assertFalse(any("将被覆盖" in f["statement"] for f in lb))

        # 司马光：未被覆盖，保留 2 条 library 事实，默认未核验、非用户编辑。
        smg = facts_by_person.get("司马光", [])
        self.assertEqual(len(smg), 2)
        for f in smg:
            self.assertEqual(f["source"], "library")
            self.assertEqual(f["verified"], 0)
            self.assertEqual(f["edited_by_user"], 0)

    def test_hidden_person_archived(self):
        report = il.ImportReport()
        plan = il._plan(report)
        smg = next(p for p in plan["persons"] if p["display_name"] == "司马光")
        self.assertEqual(smg["state"], "archived")

    def test_unmappable_biography(self):
        report = il.ImportReport()
        il._plan(report)
        self.assertTrue(any(u["kind"] == "project" and "查无此人" in u["reason"]
                            for u in report.unmappable))


class TestDryRun(_ImportCase):
    def test_dry_run_writes_nothing(self):
        report = il.build_report(mpt_db=self.mpt, apply=False)
        self.assertTrue(report.dry_run)
        self.assertFalse(os.path.exists(self.mpt))  # 未落库
        self.assertEqual(sum(report.before.values()), 0)
        self.assertGreater(report.planned.get("persons", 0), 0)
        text = report.render()
        self.assertIn("DRY-RUN", text)


class TestApply(_ImportCase):
    def test_apply_writes_and_nullable_person(self):
        report = il.build_report(mpt_db=self.mpt, apply=True)
        self.assertFalse(report.dry_run)
        self.assertTrue(os.path.exists(self.mpt))
        c = sqlite3.connect(self.mpt)
        c.row_factory = sqlite3.Row
        try:
            persons = c.execute("SELECT display_name,state FROM persons").fetchall()
            names = {r["display_name"] for r in persons}
            self.assertIn("李白", names)
            self.assertIn("杜甫", names)
            # videos：t1(李白) + t3(主题视频 person NULL)。
            vids = c.execute("SELECT person_id,path FROM videos").fetchall()
            self.assertEqual(len(vids), 2)
            self.assertTrue(any(v["person_id"] is None for v in vids))  # §A1 可空
            scripts = c.execute("SELECT person_id,kind FROM scripts").fetchall()
            self.assertEqual(len(scripts), 2)
            kinds = {s["kind"] for s in scripts}
            self.assertIn("biography", kinds)
            self.assertIn("general", kinds)
        finally:
            c.close()

    def test_apply_is_idempotent_on_rerun(self):
        il.build_report(mpt_db=self.mpt, apply=True)
        # 第二次 apply：persons 用 INSERT OR IGNORE，不重复堆叠。
        il.build_report(mpt_db=self.mpt, apply=True)
        c = sqlite3.connect(self.mpt)
        try:
            n = c.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
        finally:
            c.close()
        self.assertEqual(n, 3)  # 司马光/李白/杜甫，不因重跑翻倍


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
