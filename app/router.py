"""Application configuration - root APIRouter

Defines all FastAPI application endpoints.

Resources:
    1. https://fastapi.tiangolo.com/tutorial/bigger-applications

"""

from fastapi import APIRouter

from app.controllers import ping
from app.controllers.v1 import (
    biography,
    llm,
    options,
    project,
    settings,
    storage,
    tts,
    video,
    workbench,
)

root_api_router = APIRouter()
root_api_router.include_router(ping.router)

# v1
root_api_router.include_router(video.router)
root_api_router.include_router(llm.router)
# 前端向导（SolidJS）所需的补充能力：项目 CRUD、选项、人物库、TTS 试听。
root_api_router.include_router(project.router)
root_api_router.include_router(options.router)
root_api_router.include_router(biography.router)
root_api_router.include_router(tts.router)
# 设置页（全局配置读写 + 连接测试）与存储/缓存管理。
root_api_router.include_router(settings.router)
root_api_router.include_router(storage.router)
# 工作台（以“人物 → 任务”为中心）：人物网格聚合统计、进行中任务抽屉、任务列表。
root_api_router.include_router(workbench.router)
