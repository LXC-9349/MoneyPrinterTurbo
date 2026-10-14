from fastapi import Depends, Path, Request

from app.controllers import base
from app.controllers.v1.base import new_router
from app.models.exception import HttpException
from app.models.schema import (
    BaseResponse,
    ProjectCreateRequest,
    ProjectPatchRequest,
    VideoParams,
)
from app.services import project_store
from app.utils import utils

# 项目 CRUD 与其余 V1 接口共用同一鉴权规则；api_key 为空时放行，
# 保持本地免认证体验，管理员显式配置 Key 后自动受保护。
router = new_router(dependencies=[Depends(base.verify_token)])


@router.get("/projects", response_model=BaseResponse, summary="List projects")
def list_projects(request: Request):
    entries = project_store.list_projects()
    data = [project_store.summary_projection(e) for e in entries]
    # 最近更新优先，便于工作区列表把在编辑的项目置顶。
    data.sort(key=lambda e: e.get("updated_at") or e.get("created_at") or 0, reverse=True)
    return utils.get_response(200, {"projects": data, "total": len(data)})


@router.post("/projects", response_model=BaseResponse, summary="Create a project")
def create_project(request: Request, body: ProjectCreateRequest):
    entry = project_store.create_project(body.mode, title=body.title)
    return utils.get_response(200, entry)


@router.get(
    "/projects/{project_id}", response_model=BaseResponse, summary="Get a project"
)
def get_project(request: Request, project_id: str = Path(..., description="Project ID")):
    entry = project_store.get_entry(project_id)
    if entry is None:
        raise HttpException(
            task_id=base.get_task_id(request),
            status_code=404,
            message="project not found",
        )
    return utils.get_response(200, entry)


@router.patch(
    "/projects/{project_id}", response_model=BaseResponse, summary="Update a project"
)
def patch_project(
    request: Request,
    body: ProjectPatchRequest,
    project_id: str = Path(..., description="Project ID"),
):
    request_id = base.get_task_id(request)
    entry = project_store.get_entry(project_id)
    if entry is None:
        raise HttpException(
            task_id=request_id, status_code=404, message="project not found"
        )

    if body.draft is not None:
        # 先按 VideoParams 严格校验，避免脏 draft 落盘后让工作区/生成阶段崩页。
        try:
            params = VideoParams.model_validate(body.draft)
        except Exception as exc:
            raise HttpException(
                task_id=request_id,
                status_code=400,
                message=f"invalid draft: {exc}",
            )
        project_store.save_draft(
            project_id,
            params,
            step=body.step,
            status=body.status,
            title=body.title,
        )
    else:
        project_store.update_entry(
            project_id,
            step=body.step,
            status=body.status,
            title=body.title,
            person=body.person,
            language=body.language,
            source=body.source,
        )

    if body.bind_task_id:
        project_store.bind_task(project_id, body.bind_task_id)

    updated = project_store.get_entry(project_id)
    return utils.get_response(200, updated)


@router.delete(
    "/projects/{project_id}", response_model=BaseResponse, summary="Delete a project"
)
def delete_project(
    request: Request, project_id: str = Path(..., description="Project ID")
):
    request_id = base.get_task_id(request)
    if not project_store.delete_project(project_id):
        raise HttpException(
            task_id=request_id, status_code=404, message="project not found"
        )
    return utils.get_response(200, {"id": project_id, "deleted": True})
