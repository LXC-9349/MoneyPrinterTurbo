import sqlite3
from urllib.parse import unquote

from fastapi import Depends, Path, Query, Request

from app.controllers import base
from app.controllers.v1.base import new_router
from app.models.exception import HttpException
from app.models.schema import (
    BaseResponse,
    GroupCreateRequest,
    PersonCreateRequest,
    PersonGroupsRequest,
    PersonPatchRequest,
)
from app.services import library, person_store
from app.utils import utils

router = new_router(dependencies=[Depends(base.verify_token)])


@router.get("/library/persons", response_model=BaseResponse, summary="List persons")
def list_persons(
    request: Request,
    dynasty: str = Query("", description="按朝代筛选"),
    category: str = Query("", description="按类别筛选"),
    keyword: str = Query("", description="按姓名关键词筛选"),
    group: str = Query("", description="按分组筛选"),
    include_hidden: bool = Query(False, description="是否包含已归档（隐藏）人物"),
):
    persons = person_store.list_persons(
        dynasty=dynasty or "",
        category=category or "",
        group=group or "",
        keyword=keyword or "",
        include_hidden=include_hidden,
    )
    return utils.get_response(
        200,
        {
            "persons": persons,
            "total": len(persons),
            "dynasties": person_store.dynasty_options(),
            "categories": person_store.category_options(),
        },
    )


@router.get("/library/counts", response_model=BaseResponse, summary="Library counts")
def library_counts(request: Request):
    """资料库语料统计（文档/书籍/章节/文库人物数）。库未启用或缺表时对应项为 None。"""
    counts: dict[str, object] = {
        "documents": None,
        "books": None,
        "chapters": None,
        "persons": None,
    }
    if library.enabled():
        db = library.db_path()
        if db is not None:
            conn = sqlite3.connect(str(db), timeout=30)
            try:
                for name in ("documents", "books", "chapters", "persons"):
                    try:
                        counts[name] = conn.execute(
                            f"SELECT COUNT(*) FROM {name}"
                        ).fetchone()[0]
                    except sqlite3.Error:
                        counts[name] = None
            finally:
                conn.close()
    return utils.get_response(200, counts)


@router.get("/library/groups", response_model=BaseResponse, summary="List person groups")
def list_groups(request: Request):
    return utils.get_response(200, {"groups": person_store.list_groups()})


@router.post("/library/groups", response_model=BaseResponse, summary="Create a group")
def create_group(request: Request, body: GroupCreateRequest):
    request_id = base.get_task_id(request)
    name = body.name.strip()
    if not person_store.create_group(name):
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message="分组已存在或名称无效",
        )
    return utils.get_response(200, {"groups": person_store.list_groups()})


@router.delete(
    "/library/groups/{name}", response_model=BaseResponse, summary="Delete a group"
)
def delete_group(request: Request, name: str = Path(..., description="分组名")):
    deleted = person_store.delete_group(unquote(name).strip())
    return utils.get_response(
        200, {"deleted": bool(deleted), "groups": person_store.list_groups()}
    )


@router.post(
    "/library/persons", response_model=BaseResponse, summary="Create a custom person"
)
def create_person(request: Request, body: PersonCreateRequest):
    request_id = base.get_task_id(request)
    person = body.person.strip()
    if not person_store.create_custom_person(
        person,
        dynasty=body.dynasty,
        category=body.category,
        facts=body.facts,
        note=body.note,
    ):
        raise HttpException(
            task_id=request_id, status_code=400, message="人物名无效"
        )
    return utils.get_response(200, person_store.get_person(person))


@router.get(
    "/library/persons/{name}/documents",
    response_model=BaseResponse,
    summary="List biography source documents for a person",
)
def get_person_documents(request: Request, name: str = Path(..., description="人物姓名")):
    request_id = base.get_task_id(request)
    person = unquote(name).strip()
    profile = library.get_person_profile(person)
    if profile is None:
        raise HttpException(
            task_id=request_id,
            status_code=404,
            message="person not found",
        )
    doc_ids = library.all_doc_ids(profile)
    return utils.get_response(
        200,
        {
            "person": person,
            "dynasty": profile.get("dynasty") or "",
            "key_facts": profile.get("key_facts") or [],
            "documents": profile.get("docs") or [],
            "doc_ids": doc_ids,
            "default_base_script_ids": library.default_base_script_ids(profile),
        },
    )


@router.get(
    "/library/persons/{name}/profile",
    response_model=BaseResponse,
    summary="Get an overlay-aware person profile (library or custom)",
)
def get_person_profile(request: Request, name: str = Path(..., description="人物姓名")):
    request_id = base.get_task_id(request)
    person = unquote(name).strip()
    profile = person_store.get_person(person)
    if profile is None:
        raise HttpException(
            task_id=request_id, status_code=404, message="person not found"
        )
    return utils.get_response(200, profile)


@router.patch(
    "/library/persons/{name}", response_model=BaseResponse, summary="Update person meta"
)
def patch_person(
    request: Request,
    body: PersonPatchRequest,
    name: str = Path(..., description="人物姓名"),
):
    request_id = base.get_task_id(request)
    person = unquote(name).strip()
    if not person:
        raise HttpException(
            task_id=request_id, status_code=400, message="人物名无效"
        )
    person_store.update_person_meta(
        person,
        dynasty=body.dynasty,
        category=body.category,
        note=body.note,
        facts=body.facts,
        pinned=body.pinned,
        tags=body.tags,
    )
    return utils.get_response(200, person_store.get_person(person))


@router.put(
    "/library/persons/{name}/groups",
    response_model=BaseResponse,
    summary="Replace a person's group membership",
)
def set_person_groups(
    request: Request,
    body: PersonGroupsRequest,
    name: str = Path(..., description="人物姓名"),
):
    person = unquote(name).strip()
    person_store.set_person_groups(person, list(body.groups or []))
    return utils.get_response(
        200, {"person": person, "groups": person_store.groups_of(person)}
    )


@router.post(
    "/library/persons/{name}/restore",
    response_model=BaseResponse,
    summary="Restore an archived person",
)
def restore_person(request: Request, name: str = Path(..., description="人物姓名")):
    person = unquote(name).strip()
    restored = person_store.restore_person(person)
    return utils.get_response(200, {"person": person, "restored": bool(restored)})


@router.delete(
    "/library/persons/{name}", response_model=BaseResponse, summary="Delete/archive a person"
)
def delete_person(request: Request, name: str = Path(..., description="人物姓名")):
    person = unquote(name).strip()
    outcome = person_store.delete_person(person)
    return utils.get_response(200, {"person": person, "outcome": outcome})
