"""里程碑 4 #5：分阶段确认（awaiting_confirmation）+ 检查点续跑 + 分镜编辑。

覆盖：
* 脚本门槛暂停 → awaiting_confirmation，检查点存脚本，任务详情可续跑；
* 素材门槛暂停 → awaiting_confirmation，检查点存配音/字幕/素材；
* resume_from=terms 复用已确认脚本，不重跑 generate_script；
* resume_from=video 复用配音/字幕/素材，不重跑脚本/关键词/TTS/检索；
* 确认服务：换图/排除/删镜写回检查点，next_resume_from 映射，确认屏逐镜视图。
"""

from unittest.mock import patch

import pytest

from app.models.schema import VideoParams
from app.services import confirmation
from app.services import task as tm
from app.services import task_artifacts
from app.services.state import MemoryState


def _params(**kw):
    base = dict(video_subject="Coffee", video_script="A supplied script", video_source="pexels")
    base.update(kw)
    return VideoParams(**base)


def _seed_checkpoint(task_id, stage, **fields):
    task_artifacts.write_script_data(
        task_id, {"confirmation_checkpoint": {"stage": stage, **fields}}
    )


# ── 流水线门槛与续跑 ─────────────────────────────────────────────────
def test_script_gate_pauses_and_checkpoints():
    state = MemoryState()
    params = _params(video_script="", pause_gates=["script"])
    with (
        patch.object(tm.sm, "state", state),
        patch.object(tm.utils, "check_ffmpeg_ready", return_value=True),
        patch.object(tm, "generate_script", return_value="the confirmed script"),
        patch.object(tm, "generate_terms", return_value=["beans"]) as terms,
        patch.object(tm, "generate_audio") as audio,
    ):
        result = tm.start("t-script-gate", params, stop_at="video")

    assert result["state"] == tm.const.TASK_STATE_AWAITING_CONFIRMATION
    assert result["paused_stage"] == "script"
    assert state.get_task("t-script-gate")["state"] == tm.const.TASK_STATE_AWAITING_CONFIRMATION
    # 脚本门槛在关键词/配音之前就返回，下游一个都不该被调用。
    terms.assert_not_called()
    audio.assert_not_called()
    ckpt = confirmation.read_checkpoint("t-script-gate")
    assert ckpt["stage"] == "script"
    assert ckpt["script"] == "the confirmed script"


def test_resume_from_terms_reuses_confirmed_script():
    _seed_checkpoint("t-resume-terms", "script", script="already confirmed")
    state = MemoryState()
    params = _params(video_script="", resume_from="terms")
    with (
        patch.object(tm.sm, "state", state),
        patch.object(tm.utils, "check_ffmpeg_ready", return_value=True),
        patch.object(tm, "generate_script", side_effect=AssertionError("must not regenerate")) as script,
        patch.object(tm, "generate_terms", return_value=["beans"]) as terms,
        patch.object(tm, "save_script_data"),
        patch.object(tm, "generate_audio", return_value=("a.mp3", 5.0, object())),
        patch.object(tm, "generate_subtitle", return_value="s.srt"),
        patch.object(tm, "get_video_materials", return_value=["m1", "m2"]),
        patch.object(tm, "generate_final_videos", return_value=(["final-1.mp4"], ["c-1.mp4"], [])),
    ):
        result = tm.start("t-resume-terms", params, stop_at="video")

    script.assert_not_called()
    terms.assert_called_once()
    # 成功完成时返回体不含 state 键（终态写入状态存储），断言从 state 读取。
    assert state.get_task("t-resume-terms")["state"] == tm.const.TASK_STATE_COMPLETE


def test_materials_gate_pauses_with_all_artifacts():
    state = MemoryState()
    params = _params(video_script="", pause_gates=["materials"])
    with (
        patch.object(tm.sm, "state", state),
        patch.object(tm.utils, "check_ffmpeg_ready", return_value=True),
        patch.object(tm, "generate_script", return_value="S"),
        patch.object(tm, "generate_terms", return_value=["beans"]),
        patch.object(tm, "save_script_data"),
        patch.object(tm, "generate_audio", return_value=("a.mp3", 7.5, object())),
        patch.object(tm, "generate_subtitle", return_value="s.srt"),
        patch.object(tm, "get_video_materials", return_value=["m1", "m2"]),
        patch.object(tm, "generate_final_videos") as final_videos,
    ):
        result = tm.start("t-materials-gate", params, stop_at="video")

    assert result["state"] == tm.const.TASK_STATE_AWAITING_CONFIRMATION
    assert result["paused_stage"] == "materials"
    # 素材门槛在成片之前就返回。
    final_videos.assert_not_called()
    ckpt = confirmation.read_checkpoint("t-materials-gate")
    assert ckpt["stage"] == "materials"
    assert ckpt["materials"] == ["m1", "m2"]
    assert ckpt["audio_file"] == "a.mp3"
    assert ckpt["audio_duration"] == 7.5
    assert ckpt["subtitle_path"] == "s.srt"
    assert ckpt["script"] == "S"


def test_resume_from_video_reuses_everything_and_edited_materials():
    _seed_checkpoint(
        "t-resume-video",
        "materials",
        script="S",
        terms=["beans"],
        audio_file="a.mp3",
        audio_duration=7.5,
        subtitle_path="s.srt",
        materials=["m1", "swapped.png", "m3"],
    )
    state = MemoryState()
    params = _params(video_script="", resume_from="video")
    captured = {}

    def _capture_final(task_id, params_, downloaded_videos, audio_file, subtitle_path, audio_duration, **kw):
        captured["materials"] = list(downloaded_videos)
        captured["audio_file"] = audio_file
        captured["subtitle_path"] = subtitle_path
        return (["final-1.mp4"], ["c-1.mp4"], [])

    with (
        patch.object(tm.sm, "state", state),
        patch.object(tm.utils, "check_ffmpeg_ready", return_value=True),
        patch.object(tm, "generate_script", side_effect=AssertionError("must not regenerate")) as script,
        patch.object(tm, "generate_terms", side_effect=AssertionError("must not regenerate")) as terms,
        patch.object(tm, "generate_audio", side_effect=AssertionError("must not re-tts")) as audio,
        patch.object(tm, "generate_subtitle", side_effect=AssertionError("must not re-sub")) as sub,
        patch.object(tm, "get_video_materials", side_effect=AssertionError("must not re-search")) as mats,
        patch.object(tm, "generate_final_videos", side_effect=_capture_final),
    ):
        result = tm.start("t-resume-video", params, stop_at="video")

    for not_called in (script, terms, audio, sub, mats):
        not_called.assert_not_called()
    # 编辑后的素材列表原样喂给成片阶段。
    assert captured["materials"] == ["m1", "swapped.png", "m3"]
    assert captured["audio_file"] == "a.mp3"
    assert captured["subtitle_path"] == "s.srt"
    assert state.get_task("t-resume-video")["state"] == tm.const.TASK_STATE_COMPLETE


def test_resume_video_fails_when_checkpoint_missing():
    state = MemoryState()
    params = _params(video_script="", resume_from="video")
    with (
        patch.object(tm.sm, "state", state),
        patch.object(tm.utils, "check_ffmpeg_ready", return_value=True),
        patch.object(tm, "generate_script", return_value="S"),
    ):
        result = tm.start("t-resume-empty", params, stop_at="video")
    assert result["state"] == tm.const.TASK_STATE_FAILED
    # 无检查点时续跑在第一个复用点（脚本）就发现产物缺失并失败。
    assert result["failed_stage"] == "script"


# ── 确认服务：分镜编辑 ────────────────────────────────────────────────
def test_set_material_replaces_shot_and_persists():
    _seed_checkpoint("t-edit-set", "materials", materials=["m1", "m2", "m3"])
    out = confirmation.set_material("t-edit-set", 1, "/task/swapped.png")
    assert out["materials"] == ["m1", "/task/swapped.png", "m3"]
    assert confirmation.read_checkpoint("t-edit-set")["materials"] == [
        "m1",
        "/task/swapped.png",
        "m3",
    ]


def test_exclude_material_drops_shot_and_deletes():
    _seed_checkpoint("t-edit-excl", "materials", materials=["m1", "m2", "m3"])
    out = confirmation.exclude_material("t-edit-excl", 0, person="NoSuchPerson")
    assert out["removed"] == "m1"
    assert out["materials"] == ["m2", "m3"]
    assert out["blacklisted"] is False  # 未物化人物无登记素材可拉黑，但不报错
    del_out = confirmation.delete_shot("t-edit-excl", 1)
    assert del_out["materials"] == ["m2"]


def test_edit_rejected_when_not_at_materials_stage():
    _seed_checkpoint("t-edit-wrongstage", "script", script="S")
    with pytest.raises(ValueError):
        confirmation.set_material("t-edit-wrongstage", 0, "/x.png")


def test_next_resume_from_mapping():
    assert confirmation.next_resume_from("script") == "terms"
    assert confirmation.next_resume_from("materials") == "video"


def test_build_resume_params_requires_checkpoint():
    with pytest.raises(ValueError):
        confirmation.build_resume_params("t-no-ckpt", "video")
    _seed_checkpoint("t-ckpt-ok", "materials", materials=["m1"])
    built = confirmation.build_resume_params("t-ckpt-ok", "video")
    assert built["resume_from"] == "video"
    assert built["materials"] == ["m1"]


def test_confirmation_view_builds_shot_cards():
    _seed_checkpoint(
        "t-view",
        "materials",
        script="S",
        audio_file="a.mp3",
        audio_duration=7.5,
        subtitle_path="s.srt",
        materials=["m1.png", "m2.png"],
    )
    task_artifacts.patch_script_data(
        "t-view",
        storyboard={
            "shots": [
                {"narration": "one", "visual_query": "q1", "visual_type": "historical_photo"},
                {"narration": "two", "visual_query": "q2", "visual_type": "b_roll"},
            ]
        },
        params={"biography_person": "曹操", "video_subject": "曹操"},
    )
    canned = {
        "shots": [
            {"candidates": [{"passes_gate": True, "excluded": False, "relevance": 0.9}]},
            {"candidates": []},
        ]
    }
    with patch(
        "app.services.preview.storyboard_preview", return_value=canned
    ):
        view = confirmation.confirmation_view("t-view")

    assert view["paused_stage"] == "materials"
    assert view["editable"] is True
    assert view["person"] == "曹操"
    assert view["shot_count"] == 2
    assert view["independent_assets"] == 2
    assert view["coverage"] == 1.0
    assert view["shots"][0]["candidates"][0]["relevance"] == 0.9
    assert view["shots"][0]["chosen_name"] == "m1.png"
