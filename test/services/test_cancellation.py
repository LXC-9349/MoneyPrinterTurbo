"""里程碑 3 #6：取消语义——长阶段快速响应 + ffmpeg 子进程可终止。

覆盖三层：
1. cancel 登记表：置事件 / 检查抛异常 / 终止真实在跑的 subprocess。
2. video._run_concat_with_heartbeat：命中取消即时终止 ffmpeg 并抛 TaskCancelled；
   正常结束返回 CompletedProcess（returncode 可读）。
3. 流水线 start()：阶段内部（逐片段循环）命中取消收敛为 cancelled，而非 failed。
"""

from __future__ import annotations

import subprocess
import sys
import unittest

from app.services import cancel
from app.services import video as video_service


class TestCancelRegistry(unittest.TestCase):
    def setUp(self):
        cancel.deactivate("t-a")

    def tearDown(self):
        cancel.deactivate("t-a")

    def test_signal_and_check(self):
        cancel.activate("t-a")
        self.assertFalse(cancel.is_cancelled("t-a"))
        # 未取消时 check 不抛。
        cancel.check("t-a", "video")
        cancel.signal("t-a")
        self.assertTrue(cancel.is_cancelled("t-a"))
        with self.assertRaises(cancel.TaskCancelled) as raised:
            cancel.check("t-a", "video")
        self.assertEqual(raised.exception.stage, "video")

    def test_terminate_tree_kills_registered_process(self):
        cancel.activate("t-a")
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        cancel.register_proc(proc, task_id="t-a")
        self.assertIsNotNone(cancel.running_procs("t-a"))
        n = cancel.terminate_tree("t-a")
        self.assertGreaterEqual(n, 1)
        # 进程已死（poll 返回非 None），且取消事件已置位。
        self.assertIsNotNone(proc.poll())
        self.assertTrue(cancel.is_cancelled("t-a"))

    def test_deactivate_clears_state(self):
        cancel.activate("t-a")
        cancel.signal("t-a")
        cancel.deactivate("t-a")
        self.assertFalse(cancel.is_cancelled("t-a"))


class TestConcatCancellable(unittest.TestCase):
    def setUp(self):
        cancel.deactivate("cncat")
        cancel.set_current_task("cncat")
        cancel.activate("cncat")

    def tearDown(self):
        cancel.deactivate("cncat")
        cancel.set_current_task("")

    def test_normal_concat_returns_completed_process(self):
        # 一个立即成功退出的“伪 ffmpeg”：写点东西到 stdout 后 returncode 0。
        cmd = [sys.executable, "-c", "print('ok')"]
        result = video_service._run_concat_with_heartbeat(cmd, "out.mp4")
        self.assertEqual(result.returncode, 0)
        self.assertIn("ok", result.stdout)

    def test_cancel_terminates_ffmpeg_and_raises(self):
        # 预置取消：一个长时间睡眠的“伪 ffmpeg”在首个轮询间隔即被判取消并终止。
        cancel.signal("cncat")
        cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
        with self.assertRaises(cancel.TaskCancelled):
            video_service._run_concat_with_heartbeat(cmd, "out.mp4")

    def test_bad_timeout_config_raises_value_error(self):
        from unittest.mock import patch

        with patch.object(
            type(video_service.config.app), "get",
            return_value=0,
        ):
            with self.assertRaises(ValueError):
                video_service._run_concat_with_heartbeat(
                    [sys.executable, "-c", "print(1)"], "out.mp4"
                )


class TestInStageCancelConvergesCancelled(unittest.TestCase):
    """逐成片循环命中取消 → start() 收敛为 cancelled，而不是 failed。"""

    def test_check_raising_is_caught_as_cancelled(self):
        from unittest.mock import patch

        from app.models.schema import VideoParams
        from app.services import task as task_service

        tid = "in-stage-cancel"
        cancel.deactivate(tid)
        try:
            # _run_pipeline 因阶段内检查抛 TaskCancelled（逐片段/拼接命中），
            # start() 应兵底收敛：调 _finalize_cancelled + 返回 cancelled。
            with patch.object(
                task_service, "_finalize_cancelled"
            ) as finalize, patch.object(
                task_service, "_run_pipeline", side_effect=cancel.TaskCancelled(tid, "video")
            ):
                out = task_service.start(tid, VideoParams(video_subject=""))

            finalize.assert_called_once()
            self.assertTrue(out.get("cancelled"))
            self.assertEqual(out.get("state"), task_service.const.TASK_STATE_CANCELLED)
        finally:
            cancel.deactivate(tid)
            cancel.set_current_task("")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
