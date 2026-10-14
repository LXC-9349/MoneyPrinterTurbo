"""§九.3 回归：合规 sidecar 的音色/TTS 提供方/是否旁白取自任务实际参数。

旧实现读全局 ``config.app``，导致一次 ``voice_name="no-voice"`` 的静默片被误记为
"provider=edge / voice=女声 / 已配音"。write_sidecar 现在接受 params，据此推导。
全部离线，不碰 ffmpeg。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import config
from app.models.schema import VideoParams
from app.services import compliance


class TestSidecarFromTaskParams(unittest.TestCase):
    def _payload(self, params):
        with tempfile.TemporaryDirectory() as tmp:
            video = str(Path(tmp) / "final-1.mp4")
            out = compliance.write_sidecar(
                video, task_id="t", person="曹操", params=params
            )
            self.assertTrue(out)
            return json.loads(Path(out).read_text(encoding="utf-8"))

    def test_no_voice_is_not_reported_as_narration(self):
        params = VideoParams(video_subject="曹操", voice_name="no-voice")
        payload = self._payload(params)
        self.assertFalse(payload["ai_generated"]["narration"])
        self.assertEqual(payload["models"]["tts"]["provider"], "none")
        self.assertEqual(payload["models"]["tts"]["voice"], "no-voice")

    def test_edge_voice_reflects_actual_params(self):
        # 即便全局 config 配的是别的音色/提供方，也必须以任务参数为准。
        params = VideoParams(
            video_subject="曹操", voice_name="zh-CN-XiaoxiaoNeural-Female"
        )
        with patch.dict(
            config.app,
            {"tts_provider": "minimax", "voice_name": "stale-global-voice"},
            clear=False,
        ):
            payload = self._payload(params)
        self.assertTrue(payload["ai_generated"]["narration"])
        self.assertEqual(payload["models"]["tts"]["provider"], "edge")
        self.assertEqual(
            payload["models"]["tts"]["voice"], "zh-CN-XiaoxiaoNeural-Female"
        )

    def test_resolve_helper_mapping(self):
        self.assertEqual(
            compliance._resolve_tts_from_params(
                VideoParams(video_subject="曹操", voice_name="no-voice")
            ),
            ("none", "no-voice", False),
        )
        self.assertEqual(
            compliance._resolve_tts_from_params(
                VideoParams(video_subject="曹操", voice_name="zh-CN-YunxiNeural-Male")
            ),
            ("edge", "zh-CN-YunxiNeural-Male", True),
        )

    def test_legacy_call_without_params_keeps_old_behavior(self):
        # 不传 params 的历史调用仍返回 narration=True（不被本次修复破坏）。
        with tempfile.TemporaryDirectory() as tmp:
            video = str(Path(tmp) / "v.mp4")
            out = compliance.write_sidecar(video)
            payload = json.loads(Path(out).read_text(encoding="utf-8"))
        self.assertTrue(payload["ai_generated"]["narration"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
