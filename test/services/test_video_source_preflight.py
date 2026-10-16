"""素材来源 preflight 放行回归测试。

背景（真实缺陷）：``get_video_materials`` 早已完整支持 ``historical_archive``
（先取公开档案，空则按 ``material_fallback_source`` 兜底 b-roll），但它被漏加进
``_SUPPORTED_VIDEO_SOURCES``，导致传记向导强制的 ``historical_archive`` 在
preflight 阶段即被 "unsupported video source" 拒绝，整条传记流程经 API / 前端都
走不通。本测试锁定核心不变量：**凡是流水线声明支持的素材来源，preflight 必须全
部放行**，从而在任何一次新增/删除来源时守住两处列表的一致性。
"""

from unittest.mock import patch

import pytest

from app.models.schema import VideoParams
from app.services import task as tm
from app.services.state import MemoryState


class _PastPreflight(Exception):
    """哨兵异常：只要 ``generate_script`` 被调用，即证明已通过所有 preflight 分支。"""


def _run_source(source: str) -> dict:
    """把 ``source`` 送进真实流水线，只跑到 preflight 之后第一个调用点即返回。

    仅 mock 掉与 preflight 判定直接相关的外部依赖（FFmpeg、配音、各付费素材源的
    Key 探测）以及 preflight 之后的第一个业务调用 ``generate_script``，其余一律
    走真实代码，确保被测的就是 L2189 的来源成员校验本身。
    """
    state = MemoryState()
    params = VideoParams(
        video_subject="Coffee",
        video_script="A supplied script",
        video_source=source,
        voice_name="no-voice",
        subtitle_enabled=False,
    )
    with (
        patch.object(tm.sm, "state", state),
        patch.object(tm.utils, "check_ffmpeg_ready", return_value=True),
        patch.object(tm, "_normalize_voice_params", lambda p: None),
        patch.object(tm, "_voice_preflight_error", return_value=None),
        patch.object(tm.volcengine_seedance, "is_enabled", return_value=True),
        patch.object(tm.ofox, "is_enabled", return_value=True),
        patch.object(tm.metaso_minimax, "is_enabled", return_value=True),
        patch.object(tm.muapi, "is_enabled", return_value=True),
        patch.object(tm.material, "is_openai_image_enabled", return_value=True),
        patch.object(tm, "generate_script", side_effect=_PastPreflight()),
    ):
        tm.start("task-preflight", params, stop_at="materials")
    return state.get_task("task-preflight")


@pytest.mark.parametrize("source", sorted(tm._SUPPORTED_VIDEO_SOURCES))
def test_every_supported_source_is_admitted_by_preflight(source):
    task = _run_source(source)
    # 哨兵异常被 start() 归一为 pipeline 阶段失败，说明来源校验已放行、
    # 流水线走到了 generate_script。
    assert task.get("failed_stage") == "pipeline", task
    assert "unsupported video source" not in (task.get("error") or "")


def test_unknown_source_is_rejected_at_preflight():
    task = _run_source("definitely_not_a_source")
    assert task.get("failed_stage") == "preflight", task
    assert "unsupported video source" in (task.get("error") or "")


def test_historical_archive_is_a_supported_source():
    # 直接钉死本次回归的具体来源，便于评审一眼看到修复被保护。
    assert "historical_archive" in tm._SUPPORTED_VIDEO_SOURCES
