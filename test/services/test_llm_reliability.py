# LLM 可靠性（M2#8）：脚本生成超时 + 备用 Provider 自动切换 + 任务提醒留痕。
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.models.schema import VideoParams
from app.services import llm, task


class TestLlmTimeout(unittest.TestCase):
    def test_default_timeout_is_90_seconds(self):
        self.assertEqual(llm._llm_timeout_seconds({}), 90.0)

    def test_configured_timeout_is_honoured_including_string_form(self):
        self.assertEqual(llm._llm_timeout_seconds({"llm_timeout": 30}), 30.0)
        self.assertEqual(llm._llm_timeout_seconds({"llm_timeout": "45.5"}), 45.5)

    def test_invalid_timeout_falls_back_to_default_without_raising(self):
        # 超时是保护性兜底：坏配置不得阻断生成，只回退默认值。
        for bad in ("abc", 0, -5, float("nan"), float("inf"), True, None):
            self.assertEqual(llm._llm_timeout_seconds({"llm_timeout": bad}), 90.0)

    def test_generic_openai_client_receives_timeout(self):
        """兼容网关挂起正是卡在无超时的 generic OpenAI 客户端上。"""
        app_config = {"llm_provider": "openai", "openai_api_key": "k", "llm_timeout": 12}
        with patch.object(llm, "OpenAI") as openai_cls:
            openai_cls.return_value.chat.completions.create.return_value = None
            llm._generate_response("prompt", app_config=app_config)
        self.assertEqual(openai_cls.call_args.kwargs.get("timeout"), 12.0)

    def test_azure_client_receives_timeout(self):
        app_config = {
            "llm_provider": "azure",
            "azure_api_key": "k",
            "azure_base_url": "https://example.azure.com",
            "azure_model_name": "deploy",
            "llm_timeout": 20,
        }
        with patch.object(llm, "AzureOpenAI") as azure_cls:
            azure_cls.return_value.chat.completions.create.return_value = None
            llm._generate_response("prompt", app_config=app_config)
        self.assertEqual(azure_cls.call_args.kwargs.get("timeout"), 20.0)


class TestScriptFallback(unittest.TestCase):
    def test_primary_success_does_not_touch_fallback(self):
        cfg = {"llm_provider": "openai", "fallback_llm_provider": "deepseek"}
        with patch.object(
            llm, "_generate_response", return_value="主模型脚本"
        ) as generate_response:
            script, switched, provider = llm.generate_script_with_fallback(
                video_subject="咖啡", app_config=cfg
            )
        self.assertEqual((script, switched, provider), ("主模型脚本", False, ""))
        self.assertEqual(generate_response.call_count, 1)

    def test_switches_to_fallback_when_primary_exhausts_retries(self):
        cfg = {"llm_provider": "openai", "fallback_llm_provider": "deepseek"}

        def fake(prompt, app_config=None):
            if (app_config or {}).get("llm_provider") == "deepseek":
                return "备用模型脚本"
            return "Error: request timed out"

        with patch.object(llm, "_generate_response", side_effect=fake):
            script, switched, provider = llm.generate_script_with_fallback(
                video_subject="咖啡", app_config=cfg
            )
        self.assertEqual((script, switched, provider), ("备用模型脚本", True, "deepseek"))

    def test_no_fallback_configured_returns_empty(self):
        cfg = {"llm_provider": "openai"}
        with patch.object(
            llm, "_generate_response", return_value="Error: request timed out"
        ) as generate_response:
            script, switched, provider = llm.generate_script_with_fallback(
                video_subject="咖啡", app_config=cfg
            )
        self.assertEqual((script, switched, provider), ("", False, ""))
        # 主链路用满重试，且没有第二条链路。
        self.assertEqual(generate_response.call_count, llm._max_retries)

    def test_fallback_same_as_primary_is_ignored(self):
        cfg = {"llm_provider": "openai", "fallback_llm_provider": "openai"}
        with patch.object(
            llm, "_generate_response", return_value="Error: timeout"
        ) as generate_response:
            script, switched, provider = llm.generate_script_with_fallback(
                video_subject="咖啡", app_config=cfg
            )
        self.assertEqual((script, switched, provider), ("", False, ""))
        self.assertEqual(generate_response.call_count, llm._max_retries)

    def test_unknown_fallback_provider_is_ignored(self):
        cfg = {"llm_provider": "openai", "fallback_llm_provider": "not-a-provider"}
        with patch.object(
            llm, "_generate_response", return_value="Error: timeout"
        ):
            script, switched, provider = llm.generate_script_with_fallback(
                video_subject="咖啡", app_config=cfg
            )
        self.assertEqual((script, switched, provider), ("", False, ""))

    def test_fallback_snapshot_keeps_other_config_fields(self):
        captured = {}
        cfg = {
            "llm_provider": "openai",
            "fallback_llm_provider": "deepseek",
            "deepseek_api_key": "sk-fb",
            "llm_timeout": 33,
        }

        def fake(prompt, app_config=None):
            if (app_config or {}).get("llm_provider") == "deepseek":
                captured["app_config"] = dict(app_config)
                return "备用模型脚本"
            return "Error: timeout"

        with patch.object(llm, "_generate_response", side_effect=fake):
            llm.generate_script_with_fallback(video_subject="咖啡", app_config=cfg)
        # 快照只替换 llm_provider，其余字段（含备用 Provider 自己的 Key、超时）
        # 原样透传，避免切换后落到未配置的默认链路上。
        self.assertEqual(captured["app_config"]["llm_provider"], "deepseek")
        self.assertEqual(captured["app_config"]["deepseek_api_key"], "sk-fb")
        self.assertEqual(captured["app_config"]["llm_timeout"], 33)


class TestTaskFallbackReminder(unittest.TestCase):
    def tearDown(self):
        task._llm_fallback_events.clear()

    def test_record_and_take_are_exclusive(self):
        task._record_llm_fallback("t1", "deepseek")
        events = task._take_llm_fallback_events("t1")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["code"], "llm_fallback_switched")
        self.assertIn("已切换备用模型", events[0]["message"])
        self.assertEqual(task._take_llm_fallback_events("t1"), [])

    def test_generate_script_records_reminder_when_switched(self):
        params = VideoParams(video_subject="曹操生平")
        # 关掉自动分镜：否则编排层会先走 storyboard（未被 patch 的真实链路）。
        with patch.dict(task.config.app, {"storyboard_enabled": False}), patch.object(
            llm,
            "generate_script_with_fallback",
            return_value=("成片脚本", True, "deepseek"),
        ), patch.object(task, "_content_gate_and_retry", side_effect=lambda *a, **k: "成片脚本"):
            result = task.generate_script("t-script", params)
        self.assertEqual(result, "成片脚本")
        events = task._take_llm_fallback_events("t-script")
        # 批次 C 后：切换备用模型既记 llm_fallback_switched 提醒，也记一条
        # llm_call 调用可观测事件，因此至少含这两类。
        self.assertIn(
            "llm_fallback_switched", [e["code"] for e in events]
        )
        switched = next(
            e for e in events if e["code"] == "llm_fallback_switched"
        )
        self.assertIn("deepseek", switched["message"])

    def test_failed_task_still_carries_fallback_reminder(self):
        sm_state = MagicMock()
        sm_state.get_task.return_value = None
        with patch.object(task, "sm", MagicMock(state=sm_state)), patch.object(
            task, "_sync_task_row"
        ):
            task._record_llm_fallback("t-fail", "deepseek")
            task._mark_task_failed("t-fail", "script", "failed to generate video script")
        fields = sm_state.update_task.call_args.kwargs
        warnings = fields.get("warnings") or []
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["code"], "llm_fallback_switched")
        # 取走后不留残余：失败终态是唯一写入方。
        self.assertEqual(task._take_llm_fallback_events("t-fail"), [])


if __name__ == "__main__":
    unittest.main()
