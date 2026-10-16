"""批次 D：无结构化分镜时的确定性预览分镜派生（不依赖 LLM）+ 控制器 auto_derived 标记。"""
from __future__ import annotations

from unittest.mock import patch

from app.controllers.v1 import preview as preview_controller
from app.models.schema import PreviewStoryboardRequest
from app.services import preview as preview_service


def test_derive_shots_from_script_splits_segments():
    script = "曹操早年举兵。\n\n官渡之战以少胜多。赤壁一战三分天下。"
    shots = preview_service.derive_preview_shots("曹操", script, limit=6)
    assert shots, "有脚本时应派生出预览镜"
    assert all(s["visual_type"] == "historical_photo" for s in shots)
    # 每镜检索词非空且来自旁白关键词，绝不调用 LLM。
    assert all(s["visual_query"] for s in shots)
    assert any("官渡" in s["narration"] or "官渡" in s["visual_query"] for s in shots)


def test_derive_shots_without_script_uses_person_and_era():
    with patch(
        "app.services.archives.historical_archive.person_gate_context",
        return_value={"name": "曹操", "aliases": ["孟德"], "era_keywords": ["东汉", "三国"]},
    ):
        shots = preview_service.derive_preview_shots("曹操", "", limit=6)
    assert shots[0]["visual_query"] == "曹操"
    queries = " ".join(s["visual_query"] for s in shots)
    assert "东汉" in queries or "三国" in queries


def test_derive_shots_empty_person_no_context_is_safe():
    with patch(
        "app.services.archives.historical_archive.person_gate_context",
        side_effect=RuntimeError("boom"),
    ):
        shots = preview_service.derive_preview_shots("张江", "", limit=6)
    # 档案上下文异常也不抛出，至少回落到人物名一镜。
    assert shots and shots[0]["visual_query"] == "张江"


def test_controller_derives_when_no_shots_and_flags_auto_derived():
    captured = {}

    def fake_preview(person, shots, *, threshold=None):
        captured["shots"] = shots
        return {"person": person, "shots": shots, "shot_count": len(shots)}

    req = _FakeRequest()
    body = PreviewStoryboardRequest(person="曹操", shots=[], video_script="官渡之战。赤壁之败。")
    with patch.object(preview_service, "storyboard_preview", side_effect=fake_preview):
        resp = preview_controller.preview_storyboard(req, body)
    assert captured["shots"], "无 shots 时应派生"
    assert resp["data"]["auto_derived"] is True


def test_controller_keeps_provided_shots_not_derived():
    captured = {}

    def fake_preview(person, shots, *, threshold=None):
        captured["shots"] = shots
        return {"person": person, "shots": shots, "shot_count": len(shots)}

    req = _FakeRequest()
    given = [{"narration": "x", "visual_query": "y", "visual_type": "historical_photo"}]
    body = PreviewStoryboardRequest(person="曹操", shots=given)
    with patch.object(preview_service, "storyboard_preview", side_effect=fake_preview):
        resp = preview_controller.preview_storyboard(req, body)
    assert captured["shots"] == given
    assert resp["data"]["auto_derived"] is False


class _FakeRequest:
    """最小 Request 替身：控制器仅经 base.get_task_id 读取头部。"""

    headers: dict = {}
    state: object = None

    def __init__(self):
        self.state = type("S", (), {"task_id": "t-preview"})()
