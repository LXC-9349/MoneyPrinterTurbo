from pathlib import Path
from unittest.mock import patch

from app.config import config
from test.services import webui_ws as ws


ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"


def _widget_by_key(elements, key):
    return next(
        item
        for item in elements
        if str(getattr(item, "key", "")) == key
        or str(getattr(item, "key", "")).startswith(f"{key}_")
    )


def test_ofox_source_requires_confirmation_then_submits_without_secret_in_params():
    test_config = dict(
        config.app,
        llm_provider="openai",
        video_source="ofox",
        ofox_api_key="ofox-secret",
    )
    entry = ws.synth_entry(
        video_source="ofox",
        video_subject="AI office",
        video_script="AI helps people work faster.",
        video_terms="office worker, AI assistant",
    )
    with (
        patch.object(config, "app", test_config),
        patch.object(config, "try_save_config", return_value=True),
        ws.projects_patched(entry),
        patch("app.services.webui_task.submit_generation") as submit_generation,
    ):
        # 项目已携带 ofox 来源与文案，直接进入第 4 步（生成）。计费确认开关
        # 位于视觉步，但它的会话状态与生成前置校验都在 output 步读取；这里
        # 用普通会话键驱动确认，避开跨步导航时 AppTest 对孤立 widget 的回收。
        app = ws.open_workspace(ws.OUTPUT, lang="en", default_timeout=60)
        _widget_by_key(app.button, "generate_video_button").click().run()
        assert submit_generation.call_count == 0
        assert any("confirm" in str(item.value).lower() for item in app.error)

        app.session_state["ofox_confirm_charge"] = True
        _widget_by_key(app.button, "generate_video_button").click().run()

        assert submit_generation.call_count == 1, (
            [e.value for e in app.error]
            + [w.value for w in app.warning]
            + [str(x.value) for x in app.exception]
        )
        submitted_params = submit_generation.call_args.kwargs["params"]
        assert submitted_params.video_source == "ofox"
        assert "ofox-secret" not in submitted_params.model_dump_json()
        assert [str(item.value) for item in app.exception] == []
