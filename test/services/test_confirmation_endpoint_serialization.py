"""确认屏端点响应序列化回归（验收 #1 附带修复）。

背景：里程碑 4 #5 的 GET /tasks/{id}/confirmation 曾误用
response_model=TaskQueryResponse。该模型的 data 是 TaskStatusData，强制
`state:int`；而确认屏视图是自由结构（分镜卡片/覆盖率/提醒），不含 state，
于是 FastAPI 在响应序列化阶段校验失败抛 500。

既有控制器测试全部“直连端点函数”，绕过了 response_model 校验，所以这个
回归一直没被测到。这里用 TestClient 真正走 FastAPI 的响应序列化路径，锁死：
* 声明的 response_model 必须是通用信封 BaseResponse（不是 TaskQueryResponse）；
* 无 state 的自由视图能返回 200 且原样透传；
* 同一自由视图若按 TaskQueryResponse 校验必然失败（说明为何必须换信封）。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.controllers import base
from app.controllers.v1 import video
from app.models.schema import BaseResponse, TaskQueryResponse

# 代表真实确认屏结构的自由视图：分镜卡片含候选与提醒，唯独没有 state。
FREE_VIEW = {
    "task_id": "t-confirm",
    "paused_stage": "materials",
    "editable": True,
    "person": "曹操",
    "shot_count": 2,
    "coverage": 1.0,
    "independent_assets": 2,
    "audio_duration": 83.0,
    "missing_shots": [],
    "relevance_reminders": {"discarded_count": 40, "discarded": []},
    "shortage_reminders": [],
    "shots": [
        {"index": 0, "narration": "one", "candidates": [{"relevance": 0.9}]},
        {"index": 1, "narration": "two", "candidates": []},
    ],
}


class TestConfirmationRouteWiring(unittest.TestCase):
    def test_response_model_is_generic_envelope(self):
        # 直接锁定路由声明：若有人改回 TaskQueryResponse，这里立刻失败。
        route = next(
            r
            for r in video.router.routes
            if getattr(r, "path", None) == "/api/v1/tasks/{task_id}/confirmation"
        )
        self.assertIs(route.response_model, BaseResponse)


class TestConfirmationEndpointSerialization(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(video.router)
        # 免认证：确认端点本身不依赖登录，这里只隔离 verify_token 依赖。
        app.dependency_overrides[base.verify_token] = lambda: None
        self.client = TestClient(app)

    def test_free_view_returns_200_and_passes_through(self):
        with (
            patch.object(video, "_require_awaiting", return_value={"stage": "materials"}),
            patch.object(
                video.confirmation_service, "confirmation_view", return_value=dict(FREE_VIEW)
            ),
        ):
            resp = self.client.get("/api/v1/tasks/t-confirm/confirmation")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["status"], 200)
        # 覆盖率/分镜卡片/提醒原样透传，resumable_from 由端点补齐。
        self.assertEqual(body["data"]["shot_count"], 2)
        self.assertEqual(body["data"]["coverage"], 1.0)
        self.assertEqual(body["data"]["resumable_from"], "video")

    def test_same_view_would_fail_under_taskqueryresponse(self):
        # 复现根因：TaskStatusData 强制 state，自由视图缺 state 必校验失败。
        with self.assertRaises(ValidationError):
            TaskQueryResponse(status=200, message="success", data=FREE_VIEW)
        # 换用通用信封则通过——这正是修复的语义。
        ok = BaseResponse(status=200, message="success", data=FREE_VIEW)
        self.assertEqual(ok.data["shot_count"], 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
