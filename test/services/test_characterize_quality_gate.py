"""特征测试④：成片质量闸门的判定逻辑。

锁定"用 ffprobe/ffmpeg 客观校验磁盘真实文件、不达标降级为结构化警告（而非静默交付
或让成功渲染变失败）"这一核心行为。

绝大多数用例**把 probe/抽帧 mock 掉**，只验证确定性判定规则，因此在无 ffmpeg 的
CI 主单元测试阶段稳定运行；末尾附一个**真实生成视频的端到端用例**，用 skipUnless
门控 ffmpeg，缺失时跳过并注明原因。

* quality_gate.check_video_quality —— zero/缺流/分辨率/时长阈值/画幅不符/全黑 各项 issue。
* quality_gate._parse_from_ffmpeg —— 退化路径（无 ffprobe 时从 ffmpeg -i 解析）。
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import quality_gate


def _probe(duration="10.0", *, video=(1080, 1920, "h264"), audio=("aac",)):
    streams = []
    if video is not None:
        w, h, codec = video
        streams.append({"codec_type": "video", "codec_name": codec, "width": w, "height": h})
    if audio is not None:
        streams.append({"codec_type": "audio", "codec_name": audio[0]})
    return {"format": {"duration": duration}, "streams": streams}


class TestCheckVideoQuality(unittest.TestCase):
    def _run(self, probe, *, aspect="", min_duration=0, min_width=0, non_black=True):
        with patch.object(quality_gate, "probe_video", return_value=probe), patch.object(
            quality_gate, "_has_non_black_frame", return_value=non_black
        ):
            return quality_gate.check_video_quality("x.mp4", aspect, min_duration, min_width)

    def test_probe_failure_is_not_passed(self):
        r = self._run({"error": "boom"})
        self.assertFalse(r["passed"])
        self.assertTrue(any(i.startswith("probe_failed") for i in r["issues"]))

    def test_valid_9x16_passes_with_metrics(self):
        r = self._run(_probe("10.0"), aspect="9:16")
        self.assertEqual(r["issues"], [])
        self.assertTrue(r["passed"])
        self.assertEqual(r["metrics"]["duration"], 10.0)
        self.assertEqual((r["metrics"]["width"], r["metrics"]["height"]), (1080, 1920))
        self.assertEqual(r["metrics"]["audio_codec"], "aac")

    def test_zero_duration(self):
        r = self._run(_probe("0"))
        self.assertIn("zero_duration", r["issues"])

    def test_below_min_duration(self):
        r = self._run(_probe("5"), min_duration=20)
        self.assertIn("below_min_duration", r["issues"])

    def test_missing_video_stream(self):
        r = self._run(_probe("10", video=None))
        self.assertIn("missing_video_stream", r["issues"])

    def test_missing_audio_stream(self):
        r = self._run(_probe("10", audio=None))
        self.assertIn("missing_audio_stream", r["issues"])

    def test_invalid_resolution_flagged(self):
        r = self._run(_probe("10", video=(0, 0, "h264")))
        self.assertIn("invalid_resolution", r["issues"])

    def test_below_min_width(self):
        r = self._run(_probe("10", video=(640, 1138, "h264")), min_width=1000)
        self.assertIn("below_min_width", r["issues"])

    def test_aspect_mismatch_recorded_but_not_judged_failing(self):
        # 编码器可能轻微对齐：画幅不符只记 metric，不判负。
        r = self._run(_probe("10", video=(1920, 1080, "h264")), aspect="9:16")
        self.assertTrue(r["passed"])
        self.assertIn("aspect_mismatch", r["metrics"])

    def test_all_black_frames(self):
        r = self._run(_probe("10"), non_black=False)
        self.assertIn("all_black_frames", r["issues"])


class TestParseFromFfmpeg(unittest.TestCase):
    def test_extracts_duration_and_streams(self):
        info = (
            "Input #0, mov,mp4, from 'x.mp4':\n"
            "  Duration: 00:00:12.34, start: 0.000000, bitrate: 100 kb/s\n"
            "  Stream #0:0: Video: h264 (High) (avc1 / 0x31637661), yuv420p, "
            "1080x1920, 90 kb/s, 30 fps\n"
            "  Stream #0:1: Audio: aac (LC) (mp4a / 0x6134706D), 44100 Hz, stereo\n"
        )
        parsed = quality_gate._parse_from_ffmpeg(info)
        self.assertAlmostEqual(float(parsed["format"]["duration"]), 12.34, places=2)
        kinds = {s["codec_type"] for s in parsed["streams"]}
        self.assertEqual(kinds, {"video", "audio"})
        video = next(s for s in parsed["streams"] if s["codec_type"] == "video")
        self.assertEqual((video["width"], video["height"]), (1080, 1920))
        self.assertEqual(video["codec_name"], "h264")

    def test_no_info_returns_zero_duration(self):
        parsed = quality_gate._parse_from_ffmpeg("")
        self.assertEqual(parsed["streams"], [])
        self.assertAlmostEqual(float(parsed["format"]["duration"]), 0.0)


@unittest.skipUnless(
    shutil.which("ffmpeg"),
    "需要 ffmpeg 生成/探测真实视频；本用例在无 ffmpeg 环境跳过（阶段 ffmpeg 就绪后再跑）",
)
class TestRealProbe(unittest.TestCase):
    def test_generated_video_probe_has_video_stream_and_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "gen.mp4")
            subprocess.run(
                [
                    "ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-shortest", out,
                ],
                check=True,
                timeout=120,
            )
            probe = quality_gate.probe_video(out)
            self.assertNotIn("error", probe)
            result = quality_gate.check_video_quality(out)
            self.assertGreater(result["metrics"]["duration"], 0)
            self.assertNotIn("missing_video_stream", result["issues"])
            self.assertNotIn("missing_audio_stream", result["issues"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
