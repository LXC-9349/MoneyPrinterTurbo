"""取消真正生效的回归测试（先修2）。

锁定三条不变量（不建真实 DB / 不跑真实流水线，全部打桩）：
1. `_task_cancelled` 以 cancel.json 标记（跨进程可靠）或内存态 CANCELLED 为权威依据。
2. `_raise_if_cancelled` 命中取消时：刷新内存终态、固定 tasks 表 cancelled、清理临时
   产物目录，并抛出 `_TaskCancelled`（阶段边界提前停线）。
3. `start()` 把 `_TaskCancelled` 当作“已取消”返回，而不是失败——绝不调用 `_mark_task_failed`
   把 cancelled 覆盖成 failed。

（“取消后即使流水线继续返回完成，tasks 表仍为已取消”的 DB 级守卫见 test_task_repo。）
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.models import const
from app.models.schema import VideoParams
from app.services import task


class TestCancelDetection(unittest.TestCase):
    def test_marker_file_means_cancelled(self):
        with (
            patch.object(task.utils, "task_dir", return_value="/tmp/tasks/tz"),
            patch("os.path.exists", return_value=True),
        ):
            self.assertTrue(task._task_cancelled("tz"))

    def test_live_state_cancelled_means_cancelled(self):
        with (
            patch("os.path.exists", return_value=False),
            patch.object(task.sm.state, "get_task", return_value={"state": const.TASK_STATE_CANCELLED}),
        ):
            self.assertTrue(task._task_cancelled("tz"))

    def test_no_signal_is_not_cancelled(self):
        with (
            patch("os.path.exists", return_value=False),
            patch.object(task.sm.state, "get_task", return_value={"state": const.TASK_STATE_PROCESSING}),
        ):
            self.assertFalse(task._task_cancelled("tz"))


class TestRaiseIfCancelled(unittest.TestCase):
    def test_marks_state_and_table_and_cleans(self):
        with (
            patch.object(task, "_task_cancelled", return_value=True),
            patch.object(task.sm.state, "patch_task") as patch_task,
            patch("app.services.task_repo.mark_cancelled") as mark_cancelled,
            patch.object(task.utils, "task_dir", return_value="/tmp/tasks/tz"),
            patch("os.path.isdir", return_value=True),
            patch("shutil.rmtree") as rmtree,
        ):
            with self.assertRaises(task._TaskCancelled):
                task._raise_if_cancelled("tz", "audio")
        # 内存态被刷成 CANCELLED；tasks 表被固定 cancelled；临时目录被清理。
        patch_task.assert_called_once()
        self.assertEqual(patch_task.call_args.kwargs["state"], const.TASK_STATE_CANCELLED)
        mark_cancelled.assert_called_once_with("tz")
        rmtree.assert_called_once_with("/tmp/tasks/tz", ignore_errors=True)

    def test_not_cancelled_is_noop(self):
        with patch.object(task, "_task_cancelled", return_value=False):
            self.assertIsNone(task._raise_if_cancelled("tz", "audio"))


class TestStartRespectsCancel(unittest.TestCase):
    def test_cancelled_returned_not_marked_failed(self):
        params = VideoParams(video_subject="曹操", voice_name="zh-CN-XiaoxiaoNeural")
        with (
            patch.object(task, "_run_pipeline", side_effect=task._TaskCancelled("tz")),
            patch.object(task, "_mark_task_failed") as mark_failed,
        ):
            res = task.start("tz", params)
        mark_failed.assert_not_called()  # 关键：取消不得被当成失败覆盖终态
        self.assertEqual(res["state"], const.TASK_STATE_CANCELLED)
        self.assertTrue(res.get("cancelled"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
