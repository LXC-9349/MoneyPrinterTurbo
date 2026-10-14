"""全局设置控制器测试：读取解析 Provider 默认值、写回并归一化、连接测试。

以 SimpleNamespace 作为 Request 替身直接调用视图函数，用普通 dict 顶替共享的
config.app / config.ui 并 mock save_config，避免污染真实 config.toml；Provider
注册表与归一化逻辑不依赖 config，走真实实现。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.controllers.v1 import settings as st
from app.models.schema import SettingsUpdateRequest


def _request():
    return SimpleNamespace(headers={"x-task-id": "req-1"})


class TestSettingsRead(unittest.TestCase):
    def test_get_settings_resolves_provider_defaults(self):
        app = {
            "llm_provider": "openai",
            "openai_api_key": "sk-abc",
            "openai_base_url": "",
            "openai_model_name": "",
        }
        with (
            patch.object(st.config, "app", app),
            patch.object(st.config, "ui", {"hide_log": True}),
        ):
            response = st.get_settings(_request())
        data = response["data"]
        self.assertEqual(data["llm"]["provider"], "openai")
        self.assertEqual(data["llm"]["api_key"], "sk-abc")
        self.assertEqual(data["llm"]["base_url_effective"], "https://api.openai.com/v1")
        self.assertEqual(data["llm"]["model_effective"], "gpt-5.5")
        self.assertTrue(data["ui"]["hide_log"])
        self.assertTrue(any(p["id"] == "openai" for p in data["llm_providers"]))


class TestSettingsWrite(unittest.TestCase):
    def _run(self, body, app=None, ui=None):
        app = {} if app is None else app
        ui = {} if ui is None else ui
        with (
            patch.object(st.config, "app", app),
            patch.object(st.config, "ui", ui),
            patch.object(st.config, "save_config") as save,
        ):
            response = st.update_settings(_request(), body)
        return app, ui, save, response

    def test_update_switches_provider_and_normalizes_override(self):
        # deepseek 默认 base_url/model：命中默认值应归一化为空串，自定义模型保留。
        body = SettingsUpdateRequest(
            llm_provider="deepseek",
            llm_api_key="  key1  ",
            llm_base_url="https://api.deepseek.com",
            llm_model_name="custom-m",
        )
        app, _ui, save, _resp = self._run(body)
        self.assertEqual(app["llm_provider"], "deepseek")
        self.assertEqual(app["deepseek_api_key"], "key1")
        self.assertEqual(app["deepseek_base_url"], "")
        self.assertEqual(app["deepseek_model_name"], "custom-m")
        save.assert_called_once()

    def test_update_material_keys_split_to_list(self):
        body = SettingsUpdateRequest(pexels_api_keys="a, b,c")
        app, _ui, _save, _resp = self._run(body)
        self.assertEqual(app["pexels_api_keys"], ["a", "b", "c"])

    def test_update_empty_material_clears_list(self):
        body = SettingsUpdateRequest(pixabay_api_keys="")
        app, _ui, _save, _resp = self._run(body)
        self.assertEqual(app["pixabay_api_keys"], [])

    def test_update_hide_log_writes_ui(self):
        body = SettingsUpdateRequest(hide_log=True)
        _app, ui, _save, _resp = self._run(body)
        self.assertTrue(ui["hide_log"])

    def test_update_does_not_touch_unset_fields(self):
        body = SettingsUpdateRequest(api_key="admin")
        app, _ui, _save, _resp = self._run(body, app={"llm_provider": "openai"})
        self.assertEqual(app["api_key"], "admin")
        # 未传的 Provider 字段不应被写入。
        self.assertNotIn("openai_api_key", app)


class TestSettingsLlmTest(unittest.TestCase):
    def test_test_connection_forwards_tuple(self):
        with patch.object(st.llm, "test_connection", return_value=(True, "", 1.5)):
            response = st.test_llm_connection(_request())
        data = response["data"]
        self.assertTrue(data["ok"])
        self.assertEqual(data["elapsed"], 1.5)


if __name__ == "__main__":
    unittest.main()
