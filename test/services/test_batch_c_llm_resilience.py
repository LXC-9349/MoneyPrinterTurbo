"""批次 C LLM 韧性：离线单元测试（不联网、不发付费请求）。

覆盖检索词生成备用链切换、Provider 就绪判定、确定性检索词兜底、
以及任务编排层在 LLM 全失败时不判失败并留痕提醒。外部调用全部以桩替换。
"""
from __future__ import annotations

from unittest.mock import patch

from app.models.schema import VideoParams
from app.services import llm, task


# ── 检索词备用链（同构于脚本备用链）───────────────────────────────────
def test_terms_primary_success_does_not_touch_fallback():
    cfg = {"llm_provider": "openai", "fallback_llm_provider": "deepseek"}

    def fake(prompt, app_config=None):
        assert (app_config or cfg).get("llm_provider") == "openai"
        return '["temple", "warrior"]'

    with patch.object(llm, "_generate_response", side_effect=fake) as gr:
        terms, switched, provider = llm.generate_terms_with_fallback(
            video_subject="曹操", video_script="东汉末年", amount=2, app_config=cfg
        )
    assert (terms, switched, provider) == (["temple", "warrior"], False, "")
    assert gr.call_count == 1


def test_terms_switches_to_fallback_when_primary_fails():
    cfg = {"llm_provider": "openai", "fallback_llm_provider": "deepseek"}

    def fake(prompt, app_config=None):
        if (app_config or {}).get("llm_provider") == "deepseek":
            return '["battle", "scroll"]'
        return "Error: request timed out"

    with patch.object(llm, "_generate_response", side_effect=fake):
        terms, switched, provider = llm.generate_terms_with_fallback(
            video_subject="曹操", video_script="东汉末年", amount=2, app_config=cfg
        )
    assert (terms, switched, provider) == (["battle", "scroll"], True, "deepseek")


def test_terms_no_fallback_returns_empty():
    cfg = {"llm_provider": "openai"}
    with patch.object(llm, "_generate_response", return_value="Error: timeout"):
        terms, switched, provider = llm.generate_terms_with_fallback(
            video_subject="曹操", video_script="x", amount=3, app_config=cfg
        )
    assert (terms, switched, provider) == ([], False, "")


# ── Provider 就绪判定（不发网络）──────────────────────────────────────
def test_readiness_missing_key_is_not_ready():
    cfg = {"llm_provider": "openai", "openai_api_key": ""}
    status = llm.provider_readiness(cfg)
    assert status["provider"] == "openai"
    assert status["requires_api_key"] is True
    assert status["has_api_key"] is False
    assert status["ready"] is False


def test_readiness_with_key_is_ready():
    cfg = {"llm_provider": "openai", "openai_api_key": "sk-test", "llm_timeout": 42}
    status = llm.provider_readiness(cfg)
    assert status["ready"] is True
    assert status["timeout_seconds"] == 42.0


def test_readiness_reports_configured_fallback():
    cfg = {
        "llm_provider": "openai",
        "openai_api_key": "sk",
        "fallback_llm_provider": "deepseek",
    }
    status = llm.provider_readiness(cfg)
    assert status["fallback"] == "deepseek"


# ── 确定性检索词兜底：本地、不抛出 ────────────────────────────────────
def test_deterministic_terms_uses_subject_aliases_and_narration():
    params = VideoParams(video_subject="曹操生平", biography_person="曹操")
    script = "曹操，东汉末年杰出的政治家。\n他统一了中国北方。"
    with patch(
        "app.services.archives.historical_archive.person_gate_context",
        return_value={
            "name": "曹操",
            "aliases": ["Cao Cao", "孟德"],
            "era_keywords": ["three kingdoms"],
            "qid": "",
        },
    ):
        terms = task._deterministic_terms(params, script, limit=8)
    joined = " | ".join(terms)
    assert "曹操生平" in terms  # subject
    assert "曹操" in terms  # person
    assert "Cao Cao" in terms  # alias
    assert "three kingdoms" in terms  # era
    assert len(terms) <= 8


def test_deterministic_terms_never_raises_without_inputs():
    params = VideoParams(video_subject="")
    assert task._deterministic_terms(params, "") == []


# ── 任务编排：LLM 全失败时确定性兜底，不判失败并留痕 ──────────────────
def test_generate_terms_falls_back_deterministically_without_failing():
    params = VideoParams(video_subject="曹操生平", match_materials_to_script=True)
    with patch.object(
        llm, "generate_terms_with_fallback", return_value=([], False, "")
    ), patch.object(
        task, "_deterministic_terms", return_value=["兜底一", "兜底二"]
    ), patch.object(task, "_load_staged_storyboard", return_value=None):
        result = task.generate_terms("t-terms", params, "曹操，东汉末年人物。")
    assert result == ["兜底一", "兜底二"]
    events = task._take_llm_fallback_events("t-terms")
    codes = [e["code"] for e in events]
    assert "llm_call" in codes and "llm_fallback" in codes
    det = next(e for e in events if e["code"] == "llm_fallback")
    assert det.get("deterministic") is True


def test_generate_terms_records_call_event_even_on_success():
    params = VideoParams(video_subject="咖啡", match_materials_to_script=True)
    with patch.object(
        llm,
        "generate_terms_with_fallback",
        return_value=(["coffee", "bean"], False, ""),
    ), patch.object(task, "_load_staged_storyboard", return_value=None):
        result = task.generate_terms("t-ok", params, "咖啡的历史")
    assert result == ["coffee", "bean"]
    events = task._take_llm_fallback_events("t-ok")
    call = next(e for e in events if e["code"] == "llm_call")
    assert call["stage"] == "terms" and call["ok"] is True


def test_generate_script_records_llm_call_event():
    params = VideoParams(video_subject="曹操生平")
    with patch.dict(task.config.app, {"storyboard_enabled": False}), patch.object(
        llm,
        "generate_script_with_fallback",
        return_value=("成片脚本", False, ""),
    ), patch.object(
        task, "_content_gate_and_retry", side_effect=lambda *a, **k: "成片脚本"
    ):
        result = task.generate_script("t-s", params)
    assert result == "成片脚本"
    events = task._take_llm_fallback_events("t-s")
    call = next(e for e in events if e["code"] == "llm_call")
    assert call["stage"] == "script" and "elapsed_ms" in call
