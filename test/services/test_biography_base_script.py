"""传记“基础脚本”模式的端到端测试。

定位：史料库里的传记文章即基础脚本——用户在 WebUI 浏览并勾选后，
视频文案只能基于所选传记改写生成，取代系统自动挖掘的事实包。
覆盖：get_docs 读取、材料渲染与截断、自动链路降级、生成透传与溯源、
verbatim 比对抽取、任务参数清洗。
"""
import gc
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.models import schema
from app.services import biography, library, person_facts, task


def _make_docs_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE documents (
            doc_id TEXT PRIMARY KEY, source TEXT NOT NULL, category TEXT,
            title TEXT NOT NULL, path TEXT NOT NULL, body TEXT NOT NULL,
            n_chars INTEGER NOT NULL, extracted_at TEXT
        );
        """
    )
    rows = [
        ("d-1", "风云人物", "李世民传", "李世民于626年即位，年号贞观，开启贞观之治。" * 20),
        ("d-2", "野史秘闻", "玄武门之变", "626年六月四日，李世民在长安宫城玄武门发动政变。" * 20),
        ("d-3", "战史风云", "空正文", ""),
        ("d-4", "风云人物", "超长传记", "字" * 20000),
    ]
    for doc_id, category, title, body in rows:
        conn.execute(
            "INSERT INTO documents VALUES (?, 'A', ?, ?, '/x', ?, ?, NULL)",
            (doc_id, category, title, body, len(body)),
        )
    conn.commit()
    conn.close()


class _MiniLibraryTest(unittest.TestCase):
    """指向迷你史料库的公共夹具（env 覆盖 + 只读连接缓存清理）。"""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="mpt-base-script-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        _make_docs_db(self.tmpdir / "library.sqlite")
        self.enterContext(
            mock.patch.dict(
                "os.environ", {"MPT_LIBRARY_DB": str(self.tmpdir / "library.sqlite")}
            )
        )
        self.enterContext(mock.patch.object(library, "enabled", lambda: True))
        # Windows 下未关闭的 sqlite 句柄会阻止目录删除，先清缓存再删。
        self.addCleanup(library._conn.cache_clear)
        self.addCleanup(gc.collect)


class GetDocsTest(_MiniLibraryTest):
    def test_preserves_order_and_skips_missing(self):
        rows = library.get_docs(["d-2", "d-9", "d-1"])
        self.assertEqual([r["doc_id"] for r in rows], ["d-2", "d-1"])
        self.assertEqual(rows[0]["title"], "玄武门之变")
        self.assertIn("玄武门", rows[0]["body"])

    def test_empty_input_returns_empty(self):
        self.assertEqual(library.get_docs([]), [])


class BaseScriptMaterialTest(_MiniLibraryTest):
    def test_renders_selected_articles_as_sole_corpus(self):
        material = biography.base_script_material("李世民", ["d-1", "d-2"])
        self.assertTrue(material.startswith("【基础脚本 — 李世民"))
        self.assertIn("《李世民传》", material)
        self.assertIn("《玄武门之变》", material)

    def test_empty_ids_and_blank_bodies_yield_nothing(self):
        self.assertEqual(biography.base_script_material("李世民", []), "")
        self.assertEqual(biography.base_script_material("李世民", ["d-3"]), "")
        self.assertEqual(biography.base_script_material("李世民", ["missing"]), "")

    def test_per_doc_truncation_cap(self):
        material = biography.base_script_material("李世民", ["d-4"])
        self.assertLessEqual(material.count("字"), biography._BASE_SCRIPT_DOC_CHARS)

    def test_build_fact_material_base_mode_skips_auto_pack(self):
        with (
            mock.patch.object(
                person_facts,
                "build_fact_pack",
                side_effect=AssertionError("base mode must not mine the fact pack"),
            ),
            mock.patch(
                "app.services.archives.historical_archive.person_grounding",
                return_value={
                    "wikipedia_extract": "不应出现的自动摘要",
                    "wikipedia_description": "唐朝第二位皇帝",
                },
            ),
        ):
            material = biography.build_fact_material(
                "李世民", base_doc_ids=["d-1"]
            )
        self.assertIn("【基础脚本 — 李世民", material)
        # 只保留无事实含量的身份行，供 visual_query 取英文名
        self.assertIn("【人物身份】唐朝第二位皇帝", material)
        self.assertNotIn("不应出现的自动摘要", material)

    def test_build_fact_material_falls_back_when_ids_resolve_nothing(self):
        # 所选文章全部失效（库被重建等）时降级回自动链路而不是让任务失败。
        with (
            mock.patch.object(person_facts, "build_fact_pack", return_value=None),
            mock.patch(
                "app.services.archives.historical_archive.person_grounding",
                return_value={},
            ),
        ):
            material = biography.build_fact_material(
                "李世民", base_doc_ids=["missing-doc"]
            )
        self.assertNotIn("【基础脚本", material)
        self.assertIn("未检索到", material)


class GenerateStructuredScriptTest(unittest.TestCase):
    SCRIPT_JSON = json.dumps(
        {
            "hook": "他杀兄逼父，却开出贞观之治",
            "timeline": [
                {
                    "narration": "626年，李世民在长安即位。",
                    "duration_hint": 10,
                    "visual_type": "historical_photo",
                    "visual_query": "Emperor Taizong portrait",
                }
            ],
            "closing": "治世留给后人评说。",
        },
        ensure_ascii=False,
    )
    BASE_CORPUS = "【基础脚本 — 李世民】\n《李世民传》\n626年，李世民在长安即位，年号贞观。" * 30

    def test_base_mode_prompt_provenance_and_verbatim_source(self):
        prompts = []

        def fake_generate(prompt, app_config=None):
            prompts.append(prompt)
            return self.SCRIPT_JSON

        with (
            mock.patch.object(
                biography, "build_fact_material", return_value=self.BASE_CORPUS
            ),
            mock.patch("app.services.llm._generate_response", side_effect=fake_generate),
            mock.patch.object(
                person_facts,
                "build_fact_pack",
                side_effect=AssertionError("base mode must not build the fact pack"),
            ),
            mock.patch.object(
                person_facts,
                "check_verbatim_against",
                wraps=person_facts.check_verbatim_against,
            ) as verbatim_spy,
        ):
            script = biography.generate_structured_script(
                "李世民", base_doc_ids=["d-1", "d-2"]
            )
        self.assertIsNotNone(script)
        # 基础脚本原文进入 prompt 的“事实材料”段
        self.assertIn("【基础脚本 — 李世民", prompts[0])
        # 溯源：脚本记录它来自哪些基础脚本
        self.assertEqual(script["base_script_doc_ids"], ["d-1", "d-2"])
        # 事实校验以所选文章为语料：626/长安 都在语料内
        self.assertTrue(script["timeline"][0]["verified"])
        # verbatim 检查比对的是基础脚本语料而不是事实包
        verbatim_spy.assert_called_once()
        self.assertEqual(verbatim_spy.call_args.args[1], [self.BASE_CORPUS])

    def test_auto_mode_keeps_fact_pack_path(self):
        with (
            mock.patch.object(
                biography, "build_fact_material", return_value="普通自动材料"
            ),
            mock.patch(
                "app.services.llm._generate_response", return_value=self.SCRIPT_JSON
            ),
            mock.patch.object(person_facts, "build_fact_pack", return_value=None) as pack_spy,
        ):
            script = biography.generate_structured_script("李世民")
        self.assertIsNotNone(script)
        self.assertNotIn("base_script_doc_ids", script)
        pack_spy.assert_called_once_with("李世民")

    def test_result_contract_timeline_key(self):
        # 后端结构化传记脚本的对外契约：镜头列表在 "timeline" 键下，
        # 不再有 "shots"。前端消费端按此判空，曾因错检查 "shots"
        # 将后端成功误报为生成失败，故这里钉住后端结构防止漂移。
        with (
            mock.patch.object(
                biography, "build_fact_material", return_value=self.BASE_CORPUS
            ),
            mock.patch(
                "app.services.llm._generate_response", return_value=self.SCRIPT_JSON
            ),
            mock.patch.object(person_facts, "build_fact_pack", return_value=None),
        ):
            script = biography.generate_structured_script("李世民")
        self.assertIn("timeline", script)
        self.assertNotIn("shots", script)
        self.assertIn("626年", biography.script_to_narration(script))
        self.assertEqual(
            biography.script_to_search_terms(script), ["Emperor Taizong portrait"]
        )


class TaskPassthroughTest(unittest.TestCase):
    def test_generate_biography_script_forwards_cleaned_doc_ids(self):
        params = schema.VideoParams(
            video_subject="",
            biography_person="李世民",
            biography_doc_ids=[" d-1 ", "", "d-2"],
        )
        with mock.patch(
            "app.services.biography.generate_structured_script", return_value=None
        ) as gen:
            narration, script = task.generate_biography_script("t-1", params)
        self.assertEqual((narration, script), (None, None))
        kwargs = gen.call_args.kwargs
        self.assertEqual(kwargs["base_doc_ids"], ["d-1", "d-2"])

    def test_empty_selection_yields_empty_base_doc_ids(self):
        params = schema.VideoParams(video_subject="", biography_person="李世民")
        with mock.patch(
            "app.services.biography.generate_structured_script", return_value=None
        ) as gen:
            task.generate_biography_script("t-2", params)
        self.assertEqual(gen.call_args.kwargs["base_doc_ids"], [])

    def test_generate_script_skips_biography_llm_when_script_provided(self):
        # #4：用户已给出 video_script（粘贴/已有脚本）时，即便处于传记模式
        # （historical_archive / biography_person），generate_script 也必须整段跳过
        # generate_biography_script（LLM 重新生成），原样使用用户文案。
        params = schema.VideoParams(
            video_subject="曹操",
            video_script="曹操，字孟德，沛国谯县人。",
            video_source="historical_archive",
            biography_person="曹操",
        )
        with mock.patch.object(
            task,
            "generate_biography_script",
            side_effect=AssertionError(
                "biography LLM must be skipped when a script is provided"
            ),
        ):
            result = task.generate_script("t-skip-llm", params)
        self.assertEqual(result, "曹操，字孟德，沛国谯县人。")


class CheckVerbatimAgainstTest(unittest.TestCase):
    def test_flags_copying_and_allows_rewritten(self):
        source = "李世民于武德九年六月四日在长安玄武门发动政变，杀死兄弟李建成与李元吉。"
        copied = [{"narration": source}]
        flagged = person_facts.check_verbatim_against(copied, [source])
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["shot"], 0)
        self.assertIn("verbatim_risk", copied[0])

        rewritten = [{"narration": "武德末年的这场宫廷事变改变了皇位归属。"}]
        self.assertEqual(
            person_facts.check_verbatim_against(rewritten, [source]), []
        )


class SchemaTest(unittest.TestCase):
    def test_biography_doc_ids_defaults_and_roundtrip(self):
        self.assertEqual(
            schema.VideoParams(video_subject="").biography_doc_ids, []
        )
        params = schema.VideoParams(
            video_subject="", biography_doc_ids=["d-1", "d-2"]
        )
        self.assertEqual(
            params.model_dump()["biography_doc_ids"], ["d-1", "d-2"]
        )


if __name__ == "__main__":
    unittest.main()
