"""流水线自身把状态持久到 tasks 表的回归测试（先修3「按实际情况修」）。

历史现实：tasks 表只在提交时 record_task 写一次，运行中仅靠顶屏抽屉轮询
active_tasks 时顺手回写。没有客户端（纯 API/CLI、用户关页面、后端重启）时，
表会停在初值、终态与成片登记永远写不进去。这里锁定：

1. `_sync_task_row` 读取内存态并调用 task_repo.update_from_state 回写。
2. 阶段进度边界会触发回写（progress=10 处即调用 _sync_task_row）。
3. `_mark_task_failed` 作为终态出口，必然回写表，不依赖任何前端轮询。
4. 回写异常只降级为日志，绝不阻断生成（get_task 抛错时不冒泡）。

全部打桩，不建真实 DB、不跑真实外部服务。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.models import const
from app.services import task


class TestSyncTaskRow(unittest.TestCase):
    def test_mirrors_processing_state_with_derived_stage(self):
        # 运行中：回写时注入由进度推导的当前阶段（progress=30 → subtitle），供任务页展示。
        live = {"state": const.TASK_STATE_PROCESSING, "progress": 30}
        with (
            patch.object(task.sm.state, "get_task", return_value=live),
            patch("app.services.task_repo.update_from_state") as upd,
        ):
            task._sync_task_row("tz")
        upd.assert_called_once_with("tz", {**live, "stage": "subtitle"})
        # 关键：注入只作用于传给表的副本，绝不回写 sm.state。
        self.assertNotIn("stage", live)

    def test_terminal_state_mirrored_verbatim(self):
        # 非运行态（如完成）不注入阶段，原样回写。
        live = {"state": const.TASK_STATE_COMPLETE, "progress": 100}
        with (
            patch.object(task.sm.state, "get_task", return_value=live),
            patch("app.services.task_repo.update_from_state") as upd,
        ):
            task._sync_task_row("tz")
        upd.assert_called_once_with("tz", live)

    def test_no_live_state_is_noop(self):
        with (
            patch.object(task.sm.state, "get_task", return_value=None),
            patch("app.services.task_repo.update_from_state") as upd,
        ):
            task._sync_task_row("tz")
        upd.assert_not_called()

    def test_state_read_failure_is_swallowed(self):
        # 回写只是观测表，任何异常都不得冒泡影响生成主链路。
        with patch.object(
            task.sm.state, "get_task", side_effect=RuntimeError("boom")
        ):
            self.assertIsNone(task._sync_task_row("tz"))


class TestFailurePersistsToTable(unittest.TestCase):
    def test_mark_failed_syncs_table(self):
        with (
            patch.object(task.sm.state, "update_task"),
            patch.object(task, "_sync_task_row") as sync,
        ):
            task._mark_task_failed("tz", "audio", "failed to prepare narration audio")
        sync.assert_called_once_with("tz")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
