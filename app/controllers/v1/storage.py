"""存储 / 缓存管理控制器。

对应 WebUI「设置」区的存储面板：各子目录占用、视频缓存统计、预览与清理。
逻辑直接复用 cache_manager 与 utils.storage_dir，与 Streamlit 面板同源，
避免前后端各维护一份目录清单或清理策略。
"""
import os

from fastapi import Depends, Query, Request

from app.controllers import base
from app.controllers.v1.base import new_router
from app.models.schema import BaseResponse, CacheCleanRequest
from app.services import cache_manager
from app.utils import utils

router = new_router(dependencies=[Depends(base.verify_token)])

# 与 mpt_admin._STORAGE_DIRS 对齐的托管子目录清单（name 稳定，label 由前端映射）。
_STORAGE_DIRS = (
    "tasks",
    "cache_videos",
    "local_videos",
    "cache_material_search",
    "bgm",
    "library",
)


def _dir_usage(name: str) -> dict:
    d = os.path.join(utils.storage_dir(), name)
    count = 0
    size = 0
    exists = os.path.isdir(d)
    if exists:
        for rd, _dirs, files in os.walk(d):
            for fn in files:
                try:
                    size += os.path.getsize(os.path.join(rd, fn))
                    count += 1
                except OSError:
                    pass
    return {"name": name, "count": count, "size": size, "exists": exists}


@router.get("/storage/usage", response_model=BaseResponse, summary="Storage usage")
def get_storage_usage(request: Request):
    dirs = [_dir_usage(name) for name in _STORAGE_DIRS]
    total = sum(d["size"] for d in dirs)
    stats = cache_manager.get_video_cache_stats()
    return utils.get_response(
        200,
        {
            "dirs": dirs,
            "total_bytes": total,
            "video_cache_dir": cache_manager.video_cache_dir(),
            "video_cache": {
                "file_count": stats.file_count,
                "total_size": stats.total_size,
                "oldest_mtime": stats.oldest_mtime,
                "newest_mtime": stats.newest_mtime,
            },
        },
    )


@router.get(
    "/storage/video-cache/preview",
    response_model=BaseResponse,
    summary="Preview cleanable video cache",
)
def preview_video_cache(
    request: Request,
    max_age_days: int | None = Query(None, ge=1, le=3650, description="仅统计早于该天数的缓存"),
):
    stats = cache_manager.get_video_cache_stats(max_age_days=max_age_days)
    return utils.get_response(
        200, {"file_count": stats.file_count, "total_size": stats.total_size}
    )


@router.post(
    "/storage/video-cache/clean",
    response_model=BaseResponse,
    summary="Clean video cache",
)
def clean_video_cache(request: Request, body: CacheCleanRequest):
    result = cache_manager.clean_video_cache(max_age_days=body.max_age_days)
    remaining = cache_manager.get_video_cache_stats()
    return utils.get_response(
        200,
        {
            "deleted_count": result.deleted_count,
            "deleted_size": result.deleted_size,
            "failed_count": result.failed_count,
            "remaining": {
                "file_count": remaining.file_count,
                "total_size": remaining.total_size,
            },
        },
    )
