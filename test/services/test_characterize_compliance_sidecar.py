"""特征测试⑤：合规溯源 sidecar（<视频>.compliance.json）的结构与统计。

锁定"逐图来源/授权/作者 + 授权计数 + 所用模型 + 待复核镜头清单"这一合规产物契约，
以及 AI 标识文案、sidecar 命名规则。全部离线：write_sidecar 只落 JSON，不碰 ffmpeg。

* compliance.sidecar_path / _license_summary —— 纯函数。
* compliance.write_sidecar —— 生成结构完整、脱敏截断、unverified 抽取正确的 sidecar。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import config
from app.services import compliance


class TestPureHelpers(unittest.TestCase):
    def test_sidecar_path_same_stem(self):
        self.assertEqual(
            compliance.sidecar_path("/x/final-1.mp4"), "/x/final-1.compliance.json"
        )

    def test_license_summary_counts_by_name(self):
        sources = [
            {"license": "CC BY 4.0"},
            {"license": "CC BY 4.0"},
            {"license": "Public Domain"},
            {"license": ""},  # 空 → unknown
        ]
        self.assertEqual(
            compliance._license_summary(sources),
            {"CC BY 4.0": 2, "Public Domain": 1, "unknown": 1},
        )

    def test_label_text_language(self):
        # 自定义文案为空时：zh/空 → 中文，其它 → 英文。
        with patch.dict(config.app, {"ai_content_label_text": ""}, clear=False):
            zh = compliance.label_text("zh-CN")
            en = compliance.label_text("en-US")
            self.assertEqual(zh, compliance.AI_LABEL_TEXT)
            self.assertEqual(en, compliance.AI_LABEL_TEXT_EN)

    def test_label_text_custom_override(self):
        # 设了自定义文案则原样返回，覆盖语言分支。
        with patch.dict(config.app, {"ai_content_label_text": "AI 合成"}, clear=False):
            self.assertEqual(compliance.label_text("en-US"), "AI 合成")


class TestWriteSidecar(unittest.TestCase):
    def _sources(self):
        return [
            {
                "provider": "wikimedia",
                "asset_id": "File:Einstein.jpg",
                "source_page": "https://commons/…/File:Einstein.jpg",
                "license": "Public Domain",
                "license_url": "https://pd",
                "creator": {"name": "Anonymous"},
                "credit": "commons",
                "visual_type": "historical_photo",
                "local_file": "tasks/t/shot-000.jpg",
            },
            {
                "provider": "wikimedia",
                "asset_id": "File:Bohr.png",
                "source_page": "https://commons/…/File:Bohr.png",
                "license": "CC BY 4.0",
                "creator": "Niels Estate",  # 字符串形态也要兼容
                "visual_type": "historical_photo",
            },
        ]

    def _script(self):
        return {
            "timeline": [
                {"narration": "第一段", "verified": True},
                {
                    "narration": "第二段有问题的解说",
                    "verified": False,
                    "unverified_reasons": ["年份无出处"],
                },
            ],
            "facts_check": {"checked": 4, "unverified": 1},
        }

    def test_writes_full_schema(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            video = str(Path(tmp) / "final-1.mp4")
            out = compliance.write_sidecar(
                video,
                task_id="t-1",
                person="爱因斯坦",
                material_sources=self._sources(),
                script=self._script(),
                language="zh-CN",
            )
            self.assertEqual(out, compliance.sidecar_path(video))
            payload = json.loads(Path(out).read_text(encoding="utf-8"))

        self.assertEqual(payload["schema"], "biography.compliance/v1")
        self.assertEqual(payload["task_id"], "t-1")
        self.assertEqual(payload["person"], "爱因斯坦")
        self.assertEqual(payload["video_file"], "final-1.mp4")

        # AI 标识：文案非空、label_applied 为布尔。
        self.assertTrue(payload["ai_generated"]["narration"])
        self.assertTrue(payload["ai_generated"]["disclosure_label"])
        self.assertIsInstance(payload["ai_generated"]["label_applied"], bool)

        # 模型清单存在。
        self.assertIn("llm", payload["models"])
        self.assertIn("tts", payload["models"])

        # 逐图来源字段完整 + 作者兼容 dict/str 两种形态。
        self.assertEqual(len(payload["sources"]), 2)
        self.assertEqual(payload["sources"][0]["creator"], "Anonymous")
        self.assertEqual(payload["sources"][1]["creator"], "Niels Estate")
        self.assertEqual(payload["sources"][0]["license"], "Public Domain")

        # 授权计数与待复核镜头抽取。
        self.assertEqual(
            payload["license_summary"], {"Public Domain": 1, "CC BY 4.0": 1}
        )
        self.assertEqual(payload["script"]["shot_count"], 2)
        self.assertEqual(payload["script"]["facts_unverified"], 1)
        self.assertEqual(len(payload["script"]["unverified_shots"]), 1)
        self.assertEqual(
            payload["script"]["unverified_shots"][0]["reasons"], ["年份无出处"]
        )

    def test_empty_inputs_still_produce_valid_json(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            video = str(Path(tmp) / "v.mp4")
            out = compliance.write_sidecar(video)
            self.assertTrue(out)
            payload = json.loads(Path(out).read_text(encoding="utf-8"))
        self.assertEqual(payload["sources"], [])
        self.assertEqual(payload["script"]["shot_count"], 0)
        self.assertEqual(payload["license_summary"], {})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
