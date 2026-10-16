"""M4#4 预览控制器端点：分镜/试听/样片。全合成，mock 服务与建任务路径。"""
import unittest
from unittest.mock import patch

from app.controllers.v1 import preview as preview_controller
from app.models.exception import HttpException
from app.models.schema import (
    PreviewAuditionRequest,
    PreviewSampleRequest,
    PreviewStoryboardRequest,
)


class _FakeRequest:
    """控制器只需 request.headers.get('x-task-id')，用最小桩替代 FastAPI Request。"""

    headers = {}


class TestPreviewStoryboard(unittest.TestCase):
    def test_forwards_person_shots_threshold(self):
        body = PreviewStoryboardRequest(
            person="曹操",
            shots=[{"narration": "x", "visual_query": "portrait", "visual_type": "historical_photo"}],
            threshold="loose",
        )
        fake = {"coverage": 1.0, "shots": []}
        with patch.object(
            preview_controller.preview_service,
            "storyboard_preview",
            return_value=fake,
        ) as sb:
            resp = preview_controller.preview_storyboard(_FakeRequest(), body)
        sb.assert_called_once_with("曹操", body.shots, threshold="loose")
        self.assertEqual(resp["status"], 200)
        self.assertEqual(resp["data"], fake)

    def test_empty_person_rejected(self):
        body = PreviewStoryboardRequest(person="   ", shots=[])
        with self.assertRaises(HttpException):
            preview_controller.preview_storyboard(_FakeRequest(), body)


class TestPreviewAudition(unittest.TestCase):
    def test_returns_truncated_text(self):
        body = PreviewAuditionRequest(
            narration="曹操字孟德，小名阿瞒。" * 10, voice_rate=1.2, max_seconds=15
        )
        resp = preview_controller.preview_audition(_FakeRequest(), body)
        self.assertEqual(resp["status"], 200)
        self.assertTrue(resp["data"]["text"])
        self.assertEqual(resp["data"]["voice_rate"], 1.2)
        self.assertEqual(resp["data"]["max_seconds"], 15)


class TestPreviewSample(unittest.TestCase):
    def test_truncates_script_and_reuses_create_task(self):
        body = PreviewSampleRequest(
            params={
                "video_subject": "曹操",
                "biography_person": "曹操",
                "video_script": "曹操字孟德，小名阿瞒，一名吉利。" * 10,
                "voice_rate": 1.0,
            },
            max_seconds=15,
        )
        with patch.object(
            preview_controller.video_controller,
            "create_task",
            return_value={"status": 200, "data": {"task_id": "t-preview"}},
        ) as ct:
            resp = preview_controller.preview_sample(_FakeRequest(), body)
        self.assertEqual(resp["data"]["task_id"], "t-preview")
        # create_task 收到被裁短的 TaskVideoRequest，仍走 stop_at="video"。
        args, kwargs = ct.call_args
        sent_body = args[1]
        self.assertLess(len(sent_body.video_script), len(body.params["video_script"]))
        self.assertGreater(len(sent_body.video_script), 0)
        self.assertEqual(kwargs.get("stop_at"), "video")

    def test_missing_subject_rejected(self):
        body = PreviewSampleRequest(params={}, max_seconds=15)
        with self.assertRaises(HttpException):
            preview_controller.preview_sample(_FakeRequest(), body)


if __name__ == "__main__":
    unittest.main()
