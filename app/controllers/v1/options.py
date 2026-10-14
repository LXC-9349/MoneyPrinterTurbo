from fastapi import Depends, Query, Request

from app.controllers import base
from app.controllers.v1.base import new_router
from app.models.schema import BaseResponse
from app.services import options as options_service
from app.utils import utils

router = new_router(dependencies=[Depends(base.verify_token)])


@router.get(
    "/options/voices", response_model=BaseResponse, summary="List TTS voices"
)
def get_voices(request: Request, provider: str = Query("", description="TTS provider")):
    return utils.get_response(200, options_service.voices(provider or None))


@router.get(
    "/options/video-sources",
    response_model=BaseResponse,
    summary="List video material sources",
)
def get_video_sources(request: Request):
    return utils.get_response(200, options_service.video_sources())


@router.get("/options/fonts", response_model=BaseResponse, summary="List fonts")
def get_fonts(request: Request):
    return utils.get_response(200, {"fonts": options_service.fonts()})


@router.get("/options/languages", response_model=BaseResponse, summary="List languages")
def get_languages(request: Request):
    return utils.get_response(200, {"languages": options_service.languages()})
