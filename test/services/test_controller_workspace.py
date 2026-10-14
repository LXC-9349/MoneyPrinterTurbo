"""新增 V1 控制器测试：project / options / biography / tts。

沿用 test_controller_video.py 的轻量模式——以 SimpleNamespace 作为 Request 替身
直接调用视图函数，并 mock 其服务依赖，聚焦 HTTP 层的状态码/字段转发/异常映射，
不触碰真实磁盘与网络。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.controllers.v1 import biography as biography_controller
from app.controllers.v1 import options as options_controller
from app.controllers.v1 import project as project_controller
from app.controllers.v1 import tts as tts_controller
from app.models.exception import HttpException
from app.models.schema import (
    ProjectCreateRequest,
    ProjectPatchRequest,
    TtsPreviewRequest,
)


def _request():
    return SimpleNamespace(headers={"x-task-id": "request-123"})


class TestProjectController(unittest.TestCase):
    def test_list_projects_sorts_and_projects(self):
        entries = [
            {"id": "p_old", "updated_at": 1, "created_at": 1, "mode": "normal"},
            {"id": "p_new", "updated_at": 5, "created_at": 5, "mode": "biography",
             "person": "李白"},
        ]
        with patch.object(
            project_controller.project_store, "list_projects", return_value=entries
        ):
            response = project_controller.list_projects(_request())
        data = response["data"]
        self.assertEqual(data["total"], 2)
        # 最近更新置顶。
        self.assertEqual(data["projects"][0]["id"], "p_new")
        self.assertNotIn("draft", data["projects"][0])

    def test_create_project_forwards_mode_and_title(self):
        body = ProjectCreateRequest(mode="biography", title="李白传")
        with patch.object(
            project_controller.project_store,
            "create_project",
            return_value={"id": "p_1", "mode": "biography"},
        ) as create:
            response = project_controller.create_project(_request(), body)
        create.assert_called_once_with("biography", title="李白传")
        self.assertEqual(response["data"]["id"], "p_1")

    def test_get_project_missing_returns_404(self):
        with patch.object(
            project_controller.project_store, "get_entry", return_value=None
        ):
            with self.assertRaises(HttpException) as raised:
                project_controller.get_project(_request(), "p_missing")
        self.assertEqual(raised.exception.status_code, 404)

    def test_patch_project_valid_draft_saves(self):
        body = ProjectPatchRequest(draft={"video_subject": "花海", "video_language": "zh-CN"})
        with (
            patch.object(
                project_controller.project_store,
                "get_entry",
                side_effect=[{"id": "p_1"}, {"id": "p_1"}],
            ),
            patch.object(project_controller.project_store, "save_draft") as save,
        ):
            project_controller.patch_project(_request(), body, "p_1")
        args, kwargs = save.call_args
        self.assertEqual(args[0], "p_1")
        self.assertEqual(args[1].video_subject, "花海")

    def test_patch_project_invalid_draft_returns_400(self):
        # video_subject 为必填，缺字段应触发 VideoParams 校验失败。
        body = ProjectPatchRequest(draft={"video_language": "zh-CN"})
        with (
            patch.object(
                project_controller.project_store, "get_entry", return_value={"id": "p_1"}
            ),
            patch.object(project_controller.project_store, "save_draft"),
        ):
            with self.assertRaises(HttpException) as raised:
                project_controller.patch_project(_request(), body, "p_1")
        self.assertEqual(raised.exception.status_code, 400)

    def test_patch_project_without_draft_updates_meta(self):
        body = ProjectPatchRequest(step="visual", status="draft")
        with (
            patch.object(
                project_controller.project_store, "get_entry", return_value={"id": "p_1"}
            ),
            patch.object(project_controller.project_store, "update_entry") as update,
            patch.object(project_controller.project_store, "save_draft") as save,
        ):
            project_controller.patch_project(_request(), body, "p_1")
        save.assert_not_called()
        update.assert_called_once()

    def test_delete_project_missing_returns_404(self):
        with patch.object(
            project_controller.project_store, "delete_project", return_value=False
        ):
            with self.assertRaises(HttpException) as raised:
                project_controller.delete_project(_request(), "p_missing")
        self.assertEqual(raised.exception.status_code, 404)


class TestOptionsController(unittest.TestCase):
    def test_video_sources_returns_groups_and_flat(self):
        response = options_controller.get_video_sources(_request())
        data = response["data"]
        self.assertIn("groups", data)
        self.assertIn("local", data["sources"])
        self.assertIn("historical_archive", data["sources"])

    def test_languages_covers_support_locales(self):
        response = options_controller.get_languages(_request())
        codes = [item["value"] for item in response["data"]["languages"]]
        self.assertIn("zh-CN", codes)
        self.assertIn("en-US", codes)

    def test_voices_forwards_provider(self):
        with patch.object(
            options_controller.options_service,
            "voices",
            return_value={"provider": "siliconflow", "voices": []},
        ) as voices:
            response = options_controller.get_voices(_request(), provider="siliconflow")
        voices.assert_called_once_with("siliconflow")
        self.assertEqual(response["data"]["provider"], "siliconflow")


class TestBiographyController(unittest.TestCase):
    def test_list_persons_forwards_filters(self):
        with (
            patch.object(
                biography_controller.person_store,
                "list_persons",
                return_value=[{"person": "李白"}],
            ) as lst,
            patch.object(
                biography_controller.person_store, "dynasty_options", return_value=["唐"]
            ),
            patch.object(
                biography_controller.person_store, "category_options", return_value=["诗人"]
            ),
        ):
            response = biography_controller.list_persons(
                _request(), dynasty="唐", category="", keyword="李", group="", include_hidden=False
            )
        lst.assert_called_once_with(
            dynasty="唐", category="", group="", keyword="李", include_hidden=False
        )
        self.assertEqual(response["data"]["persons"][0]["person"], "李白")
        self.assertEqual(response["data"]["dynasties"], ["唐"])

    def test_person_documents_returns_profile_fields(self):
        profile = {
            "person": "李白",
            "dynasty": "唐",
            "key_facts": ["字太白"],
            "docs": [{"doc_id": "d1", "title": "将进酒"}],
        }
        with (
            patch.object(
                biography_controller.library, "get_person_profile", return_value=profile
            ),
            patch.object(
                biography_controller.library, "all_doc_ids", return_value=["d1"]
            ),
            patch.object(
                biography_controller.library,
                "default_base_script_ids",
                return_value=["d1"],
            ),
        ):
            response = biography_controller.get_person_documents(_request(), "李白")
        data = response["data"]
        self.assertEqual(data["documents"][0]["doc_id"], "d1")
        self.assertEqual(data["default_base_script_ids"], ["d1"])

    def test_person_documents_missing_returns_404(self):
        with patch.object(
            biography_controller.library, "get_person_profile", return_value=None
        ):
            with self.assertRaises(HttpException) as raised:
                biography_controller.get_person_documents(_request(), "无名氏")
        self.assertEqual(raised.exception.status_code, 404)


class TestTtsController(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_preview_synthesizes_and_returns_url(self):
        def fake_tts(*, text, voice_name, voice_rate, voice_file, voice_volume):
            with open(voice_file, "wb") as f:
                f.write(b"ID3fake-audio")
            return object()

        body = TtsPreviewRequest(text="你好世界", voice_name="zh-CN-XiaoxiaoNeural")
        with (
            patch.object(tts_controller, "_preview_dir", return_value=self.tmp),
            patch.object(tts_controller.voice, "tts", side_effect=fake_tts),
            patch.object(tts_controller.voice, "get_audio_duration", return_value=1.2),
        ):
            response = tts_controller.tts_preview(_request(), body)
        data = response["data"]
        self.assertTrue(data["audio_url"].startswith("/api/v1/tts/preview/preview-"))
        self.assertEqual(data["duration"], 1.2)
        # 试听文件确实落在临时目录内。
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, os.path.basename(data["audio_url"]))))

    def test_preview_no_audio_returns_502(self):
        body = TtsPreviewRequest(text="你好", voice_name="bad")
        with (
            patch.object(tts_controller, "_preview_dir", return_value=self.tmp),
            patch.object(tts_controller.voice, "tts", return_value=None),
        ):
            with self.assertRaises(HttpException) as raised:
                tts_controller.tts_preview(_request(), body)
        self.assertEqual(raised.exception.status_code, 502)

    def test_get_preview_file_serves_existing(self):
        audio = os.path.join(self.tmp, "preview-x.mp3")
        with open(audio, "wb") as f:
            f.write(b"audio")
        with (
            patch.object(tts_controller, "_preview_dir", return_value=self.tmp),
            patch.object(
                tts_controller.file_security,
                "resolve_path_within_directory",
                return_value=audio,
            ),
        ):
            response = tts_controller.get_preview_file(_request(), "preview-x.mp3")
        self.assertEqual(response.media_type, "audio/mpeg")

    def test_get_preview_file_missing_returns_404(self):
        with (
            patch.object(tts_controller, "_preview_dir", return_value=self.tmp),
            patch.object(
                tts_controller.file_security,
                "resolve_path_within_directory",
                return_value=os.path.join(self.tmp, "gone.mp3"),
            ),
        ):
            with self.assertRaises(HttpException) as raised:
                tts_controller.get_preview_file(_request(), "gone.mp3")
        self.assertEqual(raised.exception.status_code, 404)

    def test_get_preview_file_unsafe_returns_403(self):
        with (
            patch.object(tts_controller, "_preview_dir", return_value=self.tmp),
            patch.object(
                tts_controller.file_security,
                "resolve_path_within_directory",
                side_effect=ValueError("path is outside the allowed directory"),
            ),
        ):
            with self.assertRaises(HttpException) as raised:
                tts_controller.get_preview_file(_request(), "../../etc/passwd")
        self.assertEqual(raised.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
