"""M4#4 效果预览三档端点：分镜预览 / 试听文本 / 样片提交。

设计约束：
* 分镜预览、试听都**不下载、不渲染**，秒级返回，复用 preview 服务的哈希缓存；
* 样片是"独立快路径"——把旁白/脚本裁到约 15 秒后复用既有生成流水线提交一支
  短任务（含字幕样式），返回 task_id 供前端按普通任务轮询，不重造渲染逻辑；
* 参数变更后由 params_hash 自然产生新缓存键，旧结果视为"已过期"（命中不到）。
"""
from __future__ import annotations

from fastapi import Depends, Request

from app.controllers import base
from app.controllers.v1 import video as video_controller
from app.controllers.v1.base import new_router
from app.models.exception import HttpException
from app.models.schema import (
    BaseResponse,
    PreviewAuditionRequest,
    PreviewSampleRequest,
    PreviewStoryboardRequest,
    TaskVideoRequest,
)
from app.services import preview as preview_service
from app.utils import utils

router = new_router(dependencies=[Depends(base.verify_token)])


@router.post(
    "/preview/storyboard",
    response_model=BaseResponse,
    summary="Storyboard preview (no render)",
)
def preview_storyboard(request: Request, body: PreviewStoryboardRequest):
    request_id = base.get_task_id(request)
    person = (body.person or "").strip()
    if not person:
        raise HttpException(
            task_id=request_id, status_code=400, message="person is required"
        )
    result = preview_service.storyboard_preview(
        person, body.shots or [], threshold=body.threshold
    )
    return utils.get_response(200, result)


@router.post(
    "/preview/audition",
    response_model=BaseResponse,
    summary="Prepare audition text (truncate to ~seconds)",
)
def preview_audition(request: Request, body: PreviewAuditionRequest):
    request_id = base.get_task_id(request)
    text = preview_service.truncate_for_audition(
        body.narration, voice_rate=body.voice_rate, max_seconds=body.max_seconds
    )
    if not text:
        raise HttpException(
            task_id=request_id, status_code=400, message="narration is empty"
        )
    # 裁后文本交前端复用 /tts/preview 合成（所选音色/语速），此处不触网。
    return utils.get_response(
        200,
        {
            "text": text,
            "voice_rate": body.voice_rate,
            "max_seconds": body.max_seconds,
        },
    )


@router.post(
    "/preview/sample",
    response_model=BaseResponse,
    summary="Submit a short sample render (fast path)",
)
def preview_sample(request: Request, body: PreviewSampleRequest):
    request_id = base.get_task_id(request)
    params = dict(body.params or {})
    if not params.get("video_subject") and not params.get("biography_person"):
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message="video_subject or biography_person is required",
        )
    # 把旁白/脚本裁到样片窗口：短脚本 → 短 TTS → 短片，复用整条流水线。
    for key in ("video_script",):
        if params.get(key):
            params[key] = preview_service.truncate_for_audition(
                str(params[key]),
                voice_rate=float(params.get("voice_rate") or 1.0),
                max_seconds=body.max_seconds,
            )
    try:
        task_body = TaskVideoRequest(**params)
    except Exception as exc:  # noqa: BLE001 - pydantic 校验失败回 400
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message=f"invalid preview params: {exc}",
        )
    # 复用视频控制器的建任务/入队路径（含 stop_at="video" 全流程与字幕样式）。
    return video_controller.create_task(request, task_body, stop_at="video")
