"""阶段计时插桩（里程碑 3 #1）的单元测试。

覆盖 task.py 的计时原语：_begin_stage / _finish_stage / _mark_stage_done /
_timings_snapshot。落盘目录经 utils.task_dir 重定向到临时目录，tasks 表写入
用 mock 断言调用参数（表侧合并语义在 test_task_repo.TestStageTiming 覆盖）。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from app.services import task as tm
from app.utils import utils


class _Case(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        # 计时文件与 task_dir 同目录；把 task_dir 重定向到临时目录即可。
        self._p_dir = patch.object(
            utils, "task_dir", lambda sub="": self._td.name
        )
        self._p_dir.start()
        # 表写入通道只记录调用，不触碰任何真实数据库。
        self._p_repo = patch("app.services.task_repo.record_stage_timing")
        self.record = self._p_repo.start()
        # 隔离内存指针表，测试结束还原现场。
        self._saved = dict(tm._stage_current)
        tm._stage_current.clear()

    def tearDown(self):
        tm._stage_current.clear()
        tm._stage_current.update(self._saved)
        self._p_repo.stop()
        self._p_dir.stop()
        self._td.cleanup()

    def _file_timings(self):
        with open(os.path.join(self._td.name, "stage_timings.json"), encoding="utf-8") as f:
            return json.load(f)


class TestStagePrimitives(_Case):
    def test_begin_finish_writes_file_and_table(self):
        tm._begin_stage("t1", "preflight")
        tm._begin_stage("t1", "script")
        # 边界结算：preflight 已写文件与表；script 在跑，尚未结算。
        timings = self._file_timings()
        self.assertIn("preflight", timings)
        self.assertGreaterEqual(timings["preflight"], 0)
        self.assertNotIn("script", timings)
        self.record.assert_called_once()
        args = self.record.call_args.args
        self.assertEqual(args[0], "t1")
        self.assertEqual(args[1], "preflight")
        self.assertIsInstance(args[2], float)

    def test_snapshot_includes_running_stage(self):
        tm._begin_stage("t1", "preflight")
        tm._begin_stage("t1", "script")
        snap = tm._timings_snapshot("t1")
        self.assertIn("preflight", snap)
        # 在跑阶段给临时已用时，供观测表/详情时间线展示，但不落结算文件。
        self.assertIn("script", snap)
        self.assertNotIn("script", self._file_timings())

    def test_mark_stage_done_settles_last_stage(self):
        tm._begin_stage("t1", "video")
        tm._mark_stage_done("t1")
        self.assertIn("video", self._file_timings())
        self.assertNotIn("t1", tm._stage_current)
        self.record.assert_called_once_with("t1", "video", unittest.mock.ANY)

    def test_no_timing_context_is_noop(self):
        # 未走流水线的调用（历史状态/测试旁路）必须静默，不抛不写。
        self.assertIsNone(tm._timings_snapshot("ghost"))
        tm._finish_stage("ghost")
        tm._mark_stage_done("ghost")
        self.record.assert_not_called()
        self.assertFalse(
            os.path.exists(os.path.join(self._td.name, "stage_timings.json"))
        )

    def test_table_write_failure_does_not_raise(self):
        # 计时是观测配角：tasks 表写失败只记日志，文件仍要落，调用方无感。
        self.record.side_effect = RuntimeError("db locked")
        tm._begin_stage("t1", "audio")
        tm._begin_stage("t1", "subtitle")
        self.assertIn("audio", self._file_timings())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
