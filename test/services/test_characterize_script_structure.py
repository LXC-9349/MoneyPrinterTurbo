"""特征测试①：结构化脚本（storyboard / biography）的纯逻辑契约。

目的：锁定"脚本结构"这一核心行为，防止后续重构（统一数据模型）悄悄改变
分镜 JSON 的解析/归一化/提取语义。全部针对**纯函数**，不触网、不依赖 ffmpeg、
不调 LLM，因此在 CI 主单元测试阶段（未装 ffmpeg 时）稳定通过。

覆盖：
* storyboard._extract_json —— 容错解析（markdown 围栏、外层大括号截取、非 JSON）。
* storyboard._coerce_shots —— 去空、时长归一化、visual_type 固定 b_roll、"全同时长"塌陷重排。
* storyboard.storyboard_to_script —— hook/closing 与首/末分镜旁白去重。
* biography.script_to_narration / script_to_search_terms / script_shot_visual_types
  —— 三提取器的对齐规则与 visual_type 默认值。
"""

from __future__ import annotations

import unittest

from app.services import biography, storyboard


class TestExtractJson(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(storyboard._extract_json('{"a": 1}'), {"a": 1})

    def test_strips_markdown_fence_with_language(self):
        text = '```json\n{"hook": "h", "timeline": []}\n```'
        data = storyboard._extract_json(text)
        self.assertEqual(data["hook"], "h")
        self.assertEqual(data["timeline"], [])

    def test_slices_object_from_surrounding_noise(self):
        text = '前言废话 {"timeline": [{"narration": "x"}]} 结尾废话'
        data = storyboard._extract_json(text)
        self.assertEqual(data, {"timeline": [{"narration": "x"}]})

    def test_empty_and_non_json_return_empty_dict(self):
        self.assertEqual(storyboard._extract_json(""), {})
        self.assertEqual(storyboard._extract_json("完全没有 JSON"), {})


class TestCoerceShots(unittest.TestCase):
    def test_keeps_valid_shot_and_forces_b_roll(self):
        timeline = [
            {"narration": "第一镜", "visual_query": "city street", "duration_hint": 8}
        ]
        shots = storyboard._coerce_shots(timeline, target_seconds=45)
        self.assertEqual(len(shots), 1)
        self.assertEqual(shots[0]["narration"], "第一镜")
        self.assertEqual(shots[0]["visual_query"], "city street")
        self.assertEqual(shots[0]["duration_hint"], 8.0)
        # 普通视频 visual_type 恒为 b_roll，即便模型给了别的值。
        self.assertEqual(shots[0]["visual_type"], "b_roll")

    def test_drops_non_dict_and_missing_narration_or_query(self):
        timeline = [
            "字符串不是对象",
            {"narration": "", "visual_query": "q"},      # 缺旁白
            {"narration": "n", "visual_query": ""},      # 缺检索词
            {"narration": "n", "visual_query": "q", "duration_hint": 6},
        ]
        shots = storyboard._coerce_shots(timeline, target_seconds=45)
        self.assertEqual(len(shots), 1)
        self.assertEqual(shots[0]["narration"], "n")

    def test_out_of_range_hint_is_redistributed_evenly(self):
        # 时长越界（<4 或 >18）→ 用 target_seconds / len(timeline) 均分。
        timeline = [
            {"narration": "a", "visual_query": "q1", "duration_hint": 100},
            {"narration": "b", "visual_query": "q2", "duration_hint": 1},
        ]
        shots = storyboard._coerce_shots(timeline, target_seconds=40)
        for s in shots:
            self.assertEqual(s["duration_hint"], 20.0)  # 40 / 2 条

    def test_all_identical_hints_are_varied_by_narration_length(self):
        # 质量塌陷信号：>2 镜且时长全同 → 按旁白字数重排（保持总和）。
        timeline = [
            {"narration": "短", "visual_query": "q1", "duration_hint": 10},
            {"narration": "中等长度的一句解说", "visual_query": "q2", "duration_hint": 10},
            {
                "narration": "这是一个明显更长很多很多的解说句子用来占据更多时长",
                "visual_query": "q3",
                "duration_hint": 10,
            },
        ]
        shots = storyboard._coerce_shots(timeline, target_seconds=45)
        hints = [s["duration_hint"] for s in shots]
        # 不再全同，且随旁白变长而单调不减。
        self.assertGreater(len(set(hints)), 1)
        self.assertLessEqual(hints[0], hints[1])
        self.assertLessEqual(hints[1], hints[2])


class TestStoryboardToScript(unittest.TestCase):
    def test_dedups_hook_and_closing_matching_first_last_shot(self):
        sb = {
            "hook": "开场白",
            "timeline": [
                {"narration": "开场白"},   # 与 hook 实质相同 → 不重复
                {"narration": "中间"},
                {"narration": "收尾语"},   # 与 closing 实质相同 → 不重复
            ],
            "closing": "收尾语",
        }
        script = storyboard.storyboard_to_script(sb)
        self.assertEqual(script.count("开场白"), 1)
        self.assertEqual(script.count("收尾语"), 1)
        self.assertIn("中间", script)

    def test_keeps_distinct_hook_and_closing(self):
        sb = {
            "hook": "独特开场",
            "timeline": [{"narration": "正文"}],
            "closing": "独特结尾",
        }
        script = storyboard.storyboard_to_script(sb)
        self.assertEqual(script, "独特开场\n\n正文\n\n独特结尾")


class TestBiographyExtractors(unittest.TestCase):
    def setUp(self):
        self.script = {
            "hook": "H",
            "timeline": [
                {"narration": "N1", "visual_query": "q1", "visual_type": "historical_photo"},
                {"narration": "", "visual_query": "q2"},        # 有词无旁白
                {"narration": "N3", "visual_query": ""},        # 有旁白无词
                {"narration": "N4", "visual_query": "q4"},      # 缺 visual_type → 默认
            ],
            "closing": "C",
        }

    def test_terms_and_types_align_1_to_1(self):
        terms = biography.script_to_search_terms(self.script)
        types = biography.script_shot_visual_types(self.script)
        self.assertEqual(terms, ["q1", "q2", "q4"])
        self.assertEqual(len(types), len(terms))
        # 缺失 visual_type 默认 historical_photo。
        self.assertEqual(types, ["historical_photo", "historical_photo", "historical_photo"])

    def test_narration_filters_on_narration_not_query(self):
        # 旁白提取按 narration 过滤，与 terms（按 visual_query 过滤）口径不同，
        # 因此镜数可能不等——这是既有契约，须固化以防重构误改。
        narration = biography.script_to_narration(self.script)
        self.assertEqual(narration, "H\n\nN1\n\nN3\n\nN4\n\nC")
        terms = biography.script_to_search_terms(self.script)
        self.assertNotEqual(
            len(narration.split("\n\n")), len(terms)
        )

    def test_empty_script_returns_empty(self):
        self.assertEqual(biography.script_to_search_terms({}), [])
        self.assertEqual(biography.script_shot_visual_types({}), [])
        self.assertEqual(biography.script_to_narration({}), "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
