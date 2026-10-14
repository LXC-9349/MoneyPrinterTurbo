import os
from uuid import uuid4

from fastapi import Depends, Path, Request
from fastapi.responses import FileResponse
from loguru import logger

from app.controllers import base
from app.controllers.v1.base import new_router
from app.models.exception import HttpException
from app.models.schema import BaseResponse, TtsPreviewRequest
from app.services import voice
from app.utils import file_security, utils

router = new_router(dependencies=[Depends(base.verify_token)])

_PREVIEW_DIR = "temp"
_MEDIA_TYPES = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
}


def _preview_dir(create: bool = False) -> str:
    return utils.storage_dir(_PREVIEW_DIR, create=create)


@router.post(
    "/tts/preview", response_model=BaseResponse, summary="Synthesize a voice preview"
)
def tts_preview(request: Request, body: TtsPreviewRequest):
    request_id = base.get_task_id(request)
    text = (body.text or "").strip()
    if not text:
        raise HttpException(
            task_id=request_id, status_code=400, message="text is required"
        )

    preview_dir = _preview_dir(create=True)
    audio_file = os.path.join(preview_dir, f"preview-{uuid4().hex}.mp3")
    try:
        sub_maker = voice.tts(
            text=text,
            voice_name=body.voice_name,
            voice_rate=float(body.voice_rate),
            voice_file=audio_file,
            voice_volume=float(body.voice_volume),
        )
    except Exception as exc:
        logger.exception(f"tts preview failed: request_id={request_id}")
        raise HttpException(
            task_id=request_id,
            status_code=500,
            message=f"voice preview failed: {exc}",
        )

    if not sub_maker or not os.path.isfile(audio_file):
        # 合成没有产出音频文件（例如音色无效或网络失败）：清理半截文件后返回明确错误，
        # 避免前端拿到一个指向空文件的 URL。
        try:
            os.remove(audio_file)
        except OSError:
            pass
        raise HttpException(
            task_id=request_id,
            status_code=502,
            message="voice preview produced no audio",
        )

    duration = voice.get_audio_duration(audio_file)
    stored_name = os.path.basename(audio_file)
    audio_url = f"/api/v1/tts/preview/{stored_name}"
    return utils.get_response(
        200,
        {
            "audio_url": audio_url,
            "duration": duration if duration and duration > 0 else None,
            "voice_name": body.voice_name,
        },
    )


@router.get("/tts/preview/{file_name}", summary="Serve a voice preview file")
def get_preview_file(request: Request, file_name: str = Path(..., description="文件名")):
    request_id = base.get_task_id(request)
    preview_dir = _preview_dir(create=False)
    try:
        # require_file=False：把"越界/非法路径"(403)与"文件不存在"(404)分开，
        # 避免把普通的过期试听文件也报成越界。
        resolved = file_security.resolve_path_within_directory(
            preview_dir, file_name, require_file=False
        )
    except ValueError as exc:
        logger.warning(
            f"reject unsafe preview path, request_id={request_id}, "
            f"file={file_name}, error={exc}"
        )
        raise HttpException(
            task_id=request_id, status_code=403, message="invalid preview file"
        )
    if not os.path.isfile(resolved):
        raise HttpException(
            task_id=request_id, status_code=404, message="preview file not found"
        )
    extension = os.path.splitext(resolved)[1].lower()
    return FileResponse(path=resolved, media_type=_MEDIA_TYPES.get(extension, "application/octet-stream"))
