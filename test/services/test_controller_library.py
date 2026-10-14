"""素材库人物管理控制器测试：persons CRUD / groups / counts。

沿用轻量模式——以 SimpleNamespace 作为 Request 替身直接调用视图函数，mock 掉
person_store / library 依赖，聚焦 HTTP 层参数转发、结果透传与异常映射，
不触碰真实 sqlite。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.controllers.v1 import biography as bio
from app.models.exception import HttpException
from app.models.schema import (
    GroupCreateRequest,
    PersonCreateRequest,
    PersonGroupsRequest,
    PersonPatchRequest,
)


def _request():
    return SimpleNamespace(headers={"x-task-id": "req-1"})


class TestLibraryPersons(unittest.TestCase):
    def test_create_person_forwards_and_returns_profile(self):
        body = PersonCreateRequest(
            person="李白", dynasty="唐", category="诗人", note="字太白", facts=["床前明月光"]
        )
        with (
            patch.object(bio.person_store, "create_custom_person", return_value=True) as create,
            patch.object(
                bio.person_store, "get_person", return_value={"person": "李白"}
            ),
        ):
            response = bio.create_person(_request(), body)
        create.assert_called_once_with(
            "李白", dynasty="唐", category="诗人", facts=["床前明月光"], note="字太白"
        )
        self.assertEqual(response["data"]["person"], "李白")

    def test_create_person_invalid_name_returns_400(self):
        body = PersonCreateRequest(person="   ")
        with patch.object(bio.person_store, "create_custom_person", return_value=False):
            with self.assertRaises(HttpException) as raised:
                bio.create_person(_request(), body)
        self.assertEqual(raised.exception.status_code, 400)

    def test_patch_person_forwards_fields(self):
        body = PersonPatchRequest(pinned=True, dynasty="唐")
        with (
            patch.object(bio.person_store, "update_person_meta") as update,
            patch.object(
                bio.person_store, "get_person", return_value={"person": "李白", "pinned": True}
            ),
        ):
            response = bio.patch_person(_request(), body, "李白")
        kwargs = update.call_args.kwargs
        self.assertTrue(kwargs["pinned"])
        self.assertEqual(kwargs["dynasty"], "唐")
        self.assertTrue(response["data"]["pinned"])

    def test_profile_missing_returns_404(self):
        with patch.object(bio.person_store, "get_person", return_value=None):
            with self.assertRaises(HttpException) as raised:
                bio.get_person_profile(_request(), "无名氏")
        self.assertEqual(raised.exception.status_code, 404)

    def test_delete_person_returns_outcome(self):
        with patch.object(bio.person_store, "delete_person", return_value="archived"):
            response = bio.delete_person(_request(), "李白")
        self.assertEqual(response["data"]["outcome"], "archived")

    def test_restore_person_forwards(self):
        with patch.object(bio.person_store, "restore_person", return_value=True) as restore:
            response = bio.restore_person(_request(), "李白")
        restore.assert_called_once_with("李白")
        self.assertTrue(response["data"]["restored"])

    def test_set_groups_forwards(self):
        body = PersonGroupsRequest(groups=["盛唐", "诗人"])
        with (
            patch.object(bio.person_store, "set_person_groups") as setter,
            patch.object(bio.person_store, "groups_of", return_value=["盛唐", "诗人"]),
        ):
            response = bio.set_person_groups(_request(), body, "李白")
        setter.assert_called_once_with("李白", ["盛唐", "诗人"])
        self.assertEqual(response["data"]["groups"], ["盛唐", "诗人"])


class TestLibraryGroups(unittest.TestCase):
    def test_create_group_success(self):
        body = GroupCreateRequest(name="盛唐")
        with (
            patch.object(bio.person_store, "create_group", return_value=True),
            patch.object(
                bio.person_store,
                "list_groups",
                return_value=[{"name": "盛唐", "created_at": "x", "count": 0}],
            ),
        ):
            response = bio.create_group(_request(), body)
        self.assertEqual(response["data"]["groups"][0]["name"], "盛唐")

    def test_create_group_duplicate_returns_400(self):
        body = GroupCreateRequest(name="盛唐")
        with patch.object(bio.person_store, "create_group", return_value=False):
            with self.assertRaises(HttpException) as raised:
                bio.create_group(_request(), body)
        self.assertEqual(raised.exception.status_code, 400)

    def test_delete_group_reports_flag(self):
        with patch.object(bio.person_store, "delete_group", return_value=True) as deleter:
            with patch.object(bio.person_store, "list_groups", return_value=[]):
                response = bio.delete_group(_request(), "盛唐")
        deleter.assert_called_once_with("盛唐")
        self.assertTrue(response["data"]["deleted"])


class TestLibraryCounts(unittest.TestCase):
    def test_counts_disabled_library_returns_none(self):
        with patch.object(bio.library, "enabled", return_value=False):
            response = bio.library_counts(_request())
        self.assertIsNone(response["data"]["documents"])

    def test_list_persons_include_hidden_and_group(self):
        with (
            patch.object(bio.person_store, "list_persons", return_value=[]) as lst,
            patch.object(bio.person_store, "dynasty_options", return_value=[]),
            patch.object(bio.person_store, "category_options", return_value=[]),
        ):
            bio.list_persons(
                _request(), dynasty="", category="", keyword="李", group="盛唐", include_hidden=True
            )
        lst.assert_called_once_with(
            dynasty="", category="", group="盛唐", keyword="李", include_hidden=True
        )


if __name__ == "__main__":
    unittest.main()
