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


def test_muapi_source_requires_confirmation_then_submits_without_secret_in_params():
    test_config = dict(
        config.app,
        llm_provider="openai",
        video_source="muapi",
        muapi_api_key="muapi-secret",
    )
    entry = ws.synth_entry(
        video_source="muapi",
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
        app = ws.open_workspace(ws.OUTPUT, lang="en", default_timeout=60)
        _widget_by_key(app.button, "generate_video_button").click().run()
        assert submit_generation.call_count == 0
        assert any("confirm" in str(item.value).lower() for item in app.error)

        app.session_state["muapi_confirm_charge"] = True
        _widget_by_key(app.button, "generate_video_button").click().run()

        assert submit_generation.call_count == 1
        submitted_params = submit_generation.call_args.kwargs["params"]
        assert submitted_params.video_source == "muapi"
        assert "muapi-secret" not in submitted_params.model_dump_json()
        assert [str(item.value) for item in app.exception] == []
