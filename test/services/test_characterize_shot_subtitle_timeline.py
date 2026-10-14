"""特征测试②：逐镜配音时间轴 → 字幕，以及传记字幕快路径的可观测性。

锁定"逐镜 TTS 拼接与字幕时间轴"这一核心行为：
* shot_audio.timings_to_srt / _ts —— 由精确镜级时长累加得到字幕边界（构造保证对齐）。
* task._shot_timings_from_audio_dir —— 目录缺失/镜数不符时返回 None（不猜、不报错）。
* task.generate_subtitle 传记快路径：
    - 快路径生效：用逐镜时间轴直接写 subtitle.srt 并返回路径；
    - 快路径未生效：回退常规提供方，且**必须留下可观测信号**（日志），绝不静默。

不依赖 ffmpeg / 网络：探测、任务目录、时间轴均以 mock/临时目录替代。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from loguru import logger

from app.services import shot_audio, task


@contextmanager
def _capture_logs(level: str = "WARNING"):
    """收集 loguru 记录（caplog 不捕获 loguru，这里按仓库既有写法加 sink）。"""
    messages: list[str] = []
    handler_id = logger.add(
        lambda m: messages.append(str(m.record["message"])),
        level=level,
    )
    try:
        yield messages
    finally:
        logger.remove(handler_id)


def _biography_params(timeline: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(
        subtitle_enabled=True,
        biography_script={"timeline": timeline},
        subtitle_display_mode="sentence",
    )


class TestTimingsToSrt(unittest.TestCase):
    def test_cumulative_boundaries_are_exact(self):
        timings = [(0.0, 5.0, "第一镜"), (5.0, 11.0, "第二镜")]
        srt = shot_audio.timings_to_srt(timings)
        self.assertIn("00:00:00,000 --> 00:00:05,000", srt)
        self.assertIn("00:00:05,000 --> 00:00:11,000", srt)
        # 序号从 1 递增。
        self.assertIn("\n1\n", "\n" + srt)
        self.assertTrue(srt.strip().endswith("第二镜") or "第二镜" in srt)

    def test_timestamp_formatting_rounds_to_ms(self):
        self.assertEqual(shot_audio._ts(61.5), "00:01:01,500")
        self.assertEqual(shot_audio._ts(3661.004), "01:01:01,004")
        self.assertEqual(shot_audio._ts(-1), "00:00:00,000")


class TestShotTimingsFromAudioDir(unittest.TestCase):
    def test_missing_directory_returns_none(self):
        shots = [{"narration": "a"}, {"narration": "b"}]
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            task.utils, "task_dir", return_value=tmp
        ):
            # 无 audio_shots 子目录 → None（信号"用常规字幕提供方"）。
            self.assertIsNone(
                task._shot_timings_from_audio_dir("t", shots, "audio.mp3")
            )

    def test_count_mismatch_returns_none_before_probe(self):
        shots = [{"narration": "a"}, {"narration": "b"}]
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            task.utils, "task_dir", return_value=tmp
        ):
            seg_dir = Path(tmp) / "audio_shots"
            seg_dir.mkdir()
            (seg_dir / "shot-000.mp3").write_bytes(b"x")  # 只有 1 段，却有 2 段旁白
            # 数量不符 → None，且在调用 _probe_duration 之前就返回，无需 ffmpeg。
            with patch.object(shot_audio, "_probe_duration") as probe:
                result = task._shot_timings_from_audio_dir("t", shots, "audio.mp3")
            self.assertIsNone(result)
            probe.assert_not_called()


class TestGenerateSubtitleFastPath(unittest.TestCase):
    def test_effective_path_writes_srt_from_shot_timings(self):
        timeline = [{"narration": "甲"}, {"narration": "乙"}]
        timings = [(0.0, 3.0, "甲"), (3.5, 6.5, "乙")]
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(task.utils, "task_dir", return_value=tmp), patch.object(
                task, "_shot_timings_from_audio_dir", return_value=timings
            ):
                out = task.generate_subtitle("t", _biography_params(timeline), "脚本", None, "audio.mp3")
            self.assertEqual(out, os.path.join(tmp, "subtitle.srt"))
            content = Path(out).read_text(encoding="utf-8")
            self.assertIn("00:00:00,000 --> 00:00:03,000", content)
            self.assertIn("00:00:03,500 --> 00:00:06,500", content)

    def test_ineffective_path_emits_observable_signal_and_falls_back(self):
        # 快路径未生效（时间轴不可用）时不得静默：须记 warning，再走常规提供方。
        timeline = [{"narration": "甲"}]
        params = _biography_params(timeline)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(task.utils, "task_dir", return_value=tmp), patch.object(
                task, "_shot_timings_from_audio_dir", return_value=None
            ), patch.dict(task.config.app, {"subtitle_provider": "edge"}), _capture_logs() as logs:
                # sub_maker=None 且 provider=edge → 回退分支返回 ""（不下载 whisper）。
                out = task.generate_subtitle("t", params, "脚本", None, "audio.mp3")
        self.assertEqual(out, "")
        joined = "\n".join(logs)
        self.assertIn("biography shot timings unavailable", joined)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
