"""person_store 叠加层与 library 集成的后端测试。

用临时 sqlite 构造迷你史料库 + 独立个人库（env 覆盖），
覆盖分组 CRUD、自建人物、归档/恢复、overlay 合并与三维筛选。
"""
import gc
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services import library, person_store


def _make_library_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE documents (
            doc_id TEXT PRIMARY KEY, source TEXT NOT NULL, category TEXT,
            title TEXT NOT NULL, path TEXT NOT NULL, body TEXT NOT NULL,
            n_chars INTEGER NOT NULL, extracted_at TEXT
        );
        CREATE TABLE books (
            book_id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL,
            author TEXT DEFAULT '', path TEXT NOT NULL, format TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', n_chapters INTEGER DEFAULT 0,
            n_chars INTEGER DEFAULT 0, error TEXT DEFAULT '', processed_at TEXT
        );
        CREATE TABLE annotations (
            target_type TEXT NOT NULL, target_id TEXT NOT NULL, json TEXT NOT NULL,
            model TEXT, annotated_at TEXT, PRIMARY KEY (target_type, target_id)
        );
        CREATE TABLE persons (
            person TEXT PRIMARY KEY, dynasty TEXT, doc_count INTEGER,
            reliable_high INTEGER, facts_json TEXT, top_docs TEXT, books TEXT
        );
        """
    )
    docs = [
        ("d-1", "风云人物", "曹操生平"),
        ("d-2", "风云人物", "曹操煮酒论英雄"),
        ("d-3", "战史风云", "官渡之战"),
        ("d-4", "文史百科", "贞观之治"),
    ]
    for doc_id, category, title in docs:
        conn.execute(
            "INSERT INTO documents VALUES (?, 'A', ?, ?, '/x', 'body', 4, NULL)",
            (doc_id, category, title),
        )
    annos = {
        "d-1": ["曹操"],
        "d-2": ["曹操", "刘备"],
        "d-3": ["曹操"],
        "d-4": ["李世民"],
    }
    for target_id, persons in annos.items():
        payload = json.dumps(
            {"persons": persons, "key_facts": [], "dynasty": "秦汉", "reliable": "高"},
            ensure_ascii=False,
        )
        conn.execute(
            "INSERT INTO annotations VALUES ('doc', ?, ?, NULL, NULL)",
            (target_id, payload),
        )
    rows = [
        ("曹操", "秦汉", 5, 3, ["挟天子以令诸侯"], ["d-1", "d-2", "d-3"], []),
        ("李世民", "唐", 3, 2, ["贞观之治"], ["d-4"], []),
    ]
    conn.executemany(
        "INSERT INTO persons VALUES (?,?,?,?,?,?,?)",
        [
            (
                p,
                d,
                n,
                h,
                json.dumps(facts, ensure_ascii=False),
                json.dumps(top, ensure_ascii=False),
                json.dumps(bs, ensure_ascii=False),
            )
            for p, d, n, h, facts, top, bs in rows
        ],
    )
    conn.commit()
    conn.close()


class PersonStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="mpt-person-store-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        _make_library_db(self.tmpdir / "library.sqlite")
        self.enterContext(
            mock.patch.dict(
                "os.environ",
                {
                    "MPT_LIBRARY_DB": str(self.tmpdir / "library.sqlite"),
                    "MPT_PERSONAL_DB": str(self.tmpdir / "personal.sqlite"),
                },
            )
        )
        self.enterContext(mock.patch.object(library, "enabled", lambda: True))
        # Windows 下未关闭的 sqlite 句柄会阻止目录删除，先清缓存再删。
        self.addCleanup(library._conn.cache_clear)
        self.addCleanup(gc.collect)

    def test_group_crud(self):
        self.assertEqual(person_store.list_groups(), [])
        self.assertTrue(person_store.create_group("帝王系列"))
        self.assertFalse(person_store.create_group("帝王系列"))
        self.assertTrue(person_store.create_group("名臣"))
        groups = {g["name"]: g["count"] for g in person_store.list_groups()}
        self.assertEqual(groups, {"帝王系列": 0, "名臣": 0})
        person_store.set_person_groups("李世民", ["帝王系列"])
        groups = {g["name"]: g["count"] for g in person_store.list_groups()}
        self.assertEqual(groups, {"帝王系列": 1, "名臣": 0})
        self.assertTrue(person_store.delete_group("帝王系列"))
        self.assertEqual(person_store.groups_of("李世民"), [])
        self.assertEqual([g["name"] for g in person_store.list_groups()], ["名臣"])

    def test_set_person_groups_ignores_unknown_and_replaces(self):
        person_store.create_group("甲")
        person_store.set_person_groups("曹操", ["甲", "不存在的组"])
        self.assertEqual(person_store.groups_of("曹操"), ["甲"])
        person_store.set_person_groups("曹操", [])
        self.assertEqual(person_store.groups_of("曹操"), [])

    def test_custom_person_lifecycle(self):
        self.assertTrue(
            person_store.create_custom_person(
                "测试人物", dynasty="当代", facts=["事实一", "事实二"], note="备注"
            )
        )
        meta = person_store.person_meta("测试人物")
        self.assertEqual(meta["custom"], 1)
        profile = person_store.custom_profile("测试人物")
        self.assertEqual(profile["key_facts"], ["事实一", "事实二"])
        self.assertEqual(profile["note"], "备注")
        # 自建人物不依赖文库也能被 list_persons 合并出来
        listed = [r for r in person_store.list_persons() if r["person"] == "测试人物"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["source"], "custom")
        # 删除自建人物是硬删除
        self.assertEqual(person_store.delete_person("测试人物"), "deleted")
        self.assertIsNone(person_store.person_meta("测试人物"))

    def test_library_namesake_never_hard_deleted(self):
        # 与文库重名的“新建人物”实际是叠加编辑：不标记自建，
        # 删除时走归档而不是硬删叠加层，恢复后编辑不丢失。
        person_store.create_custom_person("曹操", dynasty="三国", facts=["自定义事实"])
        meta = person_store.person_meta("曹操")
        self.assertEqual(meta["custom"], 0)
        self.assertEqual(person_store.delete_person("曹操"), "archived")
        self.assertIsNone(library.get_person_profile("曹操"))
        self.assertTrue(person_store.restore_person("曹操"))
        profile = library.get_person_profile("曹操")
        self.assertEqual(profile["dynasty"], "三国")
        self.assertEqual(profile["key_facts"], ["自定义事实"])

    def test_library_overlay_merges_into_profile(self):
        person_store.update_person_meta(
            "曹操", dynasty="三国", note="乱世枭雄", facts=["编辑后的事实"]
        )
        profile = library.get_person_profile("曹操")
        self.assertEqual(profile["dynasty"], "三国")
        self.assertEqual(profile["key_facts"], ["编辑后的事实"])
        self.assertEqual(profile["note"], "乱世枭雄")
        # 未编辑的人物保持文库原值
        profile = library.get_person_profile("李世民")
        self.assertEqual(profile["dynasty"], "唐")

    def test_category_derivation_and_override(self):
        cats = person_store.person_category_map()
        # 曹操作为第一主述出现在 2 篇风云人物、1 篇战史风云 → 取众数
        self.assertEqual(cats["曹操"], "风云人物")
        profile = library.get_person_profile("曹操")
        self.assertEqual(profile["category"], "风云人物")
        person_store.update_person_meta("曹操", category="野史秘闻")
        self.assertEqual(
            library.get_person_profile("曹操")["category"], "野史秘闻"
        )

    def test_archive_restore_and_grounding_blanking(self):
        self.assertEqual(person_store.delete_person("李世民"), "archived")
        listed = [r["person"] for r in person_store.list_persons()]
        self.assertNotIn("李世民", listed)
        shown = [
            r["person"]
            for r in person_store.list_persons(include_hidden=True)
        ]
        self.assertIn("李世民", shown)
        # 归档后 grounding 链路同步失效
        self.assertIsNone(library.get_person_profile("李世民"))
        self.assertEqual(library.grounding_block("李世民"), "")
        self.assertTrue(person_store.restore_person("李世民"))
        self.assertIsNotNone(library.get_person_profile("李世民"))

    def test_list_persons_filters_and_pinned_order(self):
        names = [r["person"] for r in person_store.list_persons(dynasty="唐")]
        self.assertEqual(names, ["李世民"])
        names = [r["person"] for r in person_store.list_persons(category="风云人物")]
        self.assertEqual(names, ["曹操"])
        names = [r["person"] for r in person_store.list_persons(keyword="曹")]
        self.assertEqual(names, ["曹操"])
        person_store.create_group("帝王")
        person_store.set_person_groups("李世民", ["帝王"])
        names = [r["person"] for r in person_store.list_persons(group="帝王")]
        self.assertEqual(names, ["李世民"])
        # 置顶优先于 doc_count 排序
        person_store.update_person_meta("李世民", pinned=True)
        names = [r["person"] for r in person_store.list_persons()]
        self.assertEqual(names, ["李世民", "曹操"])

    def test_dynasty_and_category_options(self):
        # 同人数朝代按名称排序不稳定（取决于码位），只断言集合。
        self.assertEqual(set(person_store.dynasty_options()), {"秦汉", "唐"})
        person_store.update_person_meta("曹操", dynasty="三国")
        self.assertIn("三国", person_store.dynasty_options())
        self.assertEqual(
            set(person_store.category_options()),
            {"风云人物", "战史风云", "文史百科"},
        )

    def test_get_person_falls_back_to_custom(self):
        person_store.create_custom_person("马克", dynasty="当代")
        profile = person_store.get_person("马克")
        self.assertEqual(profile["source"], "custom")
        self.assertIsNone(person_store.get_person("查无此人"))
        self.assertEqual(person_store.get_person("曹操")["person"], "曹操")

    def test_works_without_library(self):
        # 文库缺失时个人库功能必须降级可用，只展示自建人物。
        with mock.patch.dict(
            "os.environ",
            {"MPT_LIBRARY_DB": str(self.tmpdir / "missing.sqlite")},
        ):
            library._conn.cache_clear()
            person_store.create_custom_person("孤本", dynasty="当代")
            listed = person_store.list_persons()
            self.assertEqual([r["person"] for r in listed], ["孤本"])

    def test_empty_library_db_returns_custom_only_for_meta_queries(self):
        # person_meta / groups 等纯个人库函数在文库缺失时也必须可用
        self.assertIsNone(person_store.person_meta("任意"))
        self.assertEqual(person_store.groups_of("任意"), [])


if __name__ == "__main__":
    unittest.main()
