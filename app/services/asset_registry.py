"""素材资产登记（里程碑 2 收尾 #2）。

流水线素材阶段完成后，把**实际下载的图片/视频素材**按内容哈希去重登记到
`assets`，并关联到人物（`person_assets`）。授权、作者、来源页、来源标题取自
`material_sources`（由素材服务在下载时写入 script.json）。传记任务优先登记
真实历史照片（`image_file`），而非临时渲染出的 mp4 片段。

设计约束：
* 属于辅助能力，**任何失败都不得阻断成片主流程**（调用方以 try/except 降级）。
* 只对"以人物为中心"的任务登记（有 biography_person 或 historical_archive），
  避免为通用素材任务把任意 video_subject 误建成人物污染 persons 表。
* `path` 以 storage 根目录为基准存相对路径（posix 分隔），与 person_repo
  `reconcile()` 的 `storage_root + path` 约定一致。
"""

from __future__ import annotations

import hashlib
import os

from loguru import logger

from app.services import person_repo
from app.utils import utils

# 扩展名 → 资产类别。图片素材进"图片素材"标签，视频片段单独归类。
_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
_VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm"}


def _kind(file_path: str) -> str:
    ext = os.path.splitext(file_path)[1].lower()
    if ext in _IMAGE_EXT:
        return "image"
    if ext in _VIDEO_EXT:
        return "video"
    return "other"


def _sha256(file_path: str) -> str:
    """流式计算内容哈希，用于跨任务去重（同图不同名也只登记一次）。"""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _storage_rel(file_path: str) -> str:
    """转成相对 storage 根目录的 posix 路径，供 reconcile/前端统一解析。"""
    root = os.path.abspath(utils.storage_dir())
    abs_path = os.path.abspath(file_path)
    try:
        rel = os.path.relpath(abs_path, root)
    except ValueError:  # 跨盘符（Windows 不同驱动器）无法取相对路径
        return abs_path.replace("\\", "/")
    return rel.replace("\\", "/")


def _author(record: dict) -> str:
    creator = record.get("creator")
    if isinstance(creator, dict) and creator.get("name"):
        return str(creator["name"]).strip()[:300]
    return str(record.get("credit") or "").strip()[:300]


def _resolve_target(record: dict, path_by_name: dict[str, str], base_dir: str) -> str:
    """把 material_source 记录映射到磁盘上的实际文件。

    传记素材优先解析 `image_file`（真实照片），退回 `local_file`（渲染片段）；
    库存素材只有 `local_file`。先按下载返回路径定位到的目录拼接，命中不了再用
    已下载文件名字典兜底。
    """
    for key in ("image_file", "local_file"):
        name = str(record.get(key) or "").strip()
        if not name:
            continue
        candidate = os.path.join(base_dir, name)
        if os.path.isfile(candidate):
            return candidate
        mapped = path_by_name.get(name)
        if mapped and os.path.isfile(mapped):
            return mapped
    return ""


def register_uploaded_image(
    person_name: str,
    file_path: str,
    *,
    source_title: str = "",
    provider: str = "upload",
    license: str = "owned",
) -> dict:
    """登记用户上传的自有图片并关联人物（里程碑 2 收尾 #3）。

    人物不存在时复用 `create_or_get_person` 惰性建档（自建），与流水线素材
    登记同一去重键 (content_hash, kind)：同内容重复上传只复用已有 asset 行，
    不覆盖其元数据。返回登记后的 asset 行 dict（含 web 可访问的 rel path）。
    """
    name = (person_name or "").strip()
    if not name:
        raise ValueError("person_name is required")
    if not file_path or not os.path.isfile(file_path):
        raise FileNotFoundError(file_path)
    content_hash = _sha256(file_path)
    conn = person_repo.connect()
    try:
        person = person_repo.create_or_get_person(name, conn=conn)
        asset_id = person_repo.upsert_asset(
            conn,
            kind=_kind(file_path),
            path=_storage_rel(file_path),
            content_hash=content_hash,
            size_bytes=os.path.getsize(file_path),
            provider=provider,
            license=license,
            source_title=source_title,
        )
        person_repo.link_person_asset(conn, person["public_id"], asset_id, state="pending")
        row = conn.execute(
            "SELECT asset_id, kind, path, provider, license, license_url, author, "
            "source_page, source_title FROM assets WHERE asset_id=?",
            (asset_id,),
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def register_material_assets(
    task_id: str,
    *,
    person_name: str,
    material_paths: list[str],
    material_sources: list[dict],
) -> dict:
    """登记下载素材为资产并关联人物；返回 {registered, linked, skipped}。"""
    name = (person_name or "").strip()
    paths = [p for p in (material_paths or []) if p and os.path.isfile(p)]
    if not name or not paths:
        return {"registered": 0, "linked": 0, "skipped": 0}

    base_dir = os.path.dirname(paths[0])
    path_by_name = {os.path.basename(p): p for p in paths}

    registered = linked = skipped = 0
    conn = person_repo.connect()
    try:
        person = person_repo.create_or_get_person(name, conn=conn)
        public_id = person["public_id"]
        person_id = person["person_id"]
        seen: set[str] = set()  # 本次任务内去重，同图多条记录只登记/关联一次
        for record in material_sources or []:
            if not isinstance(record, dict):
                continue
            target = _resolve_target(record, path_by_name, base_dir)
            if not target or not os.path.isfile(target):
                skipped += 1
                continue
            try:
                content_hash = _sha256(target)
            except OSError as exc:
                logger.warning(
                    f"asset hash failed, skip: path={target}, "
                    f"error={type(exc).__name__}: {exc}"
                )
                skipped += 1
                continue
            if content_hash in seen:
                continue
            seen.add(content_hash)

            asset_id = person_repo.upsert_asset(
                conn,
                kind=_kind(target),
                path=_storage_rel(target),
                content_hash=content_hash,
                size_bytes=os.path.getsize(target),
                provider=str(record.get("provider") or ""),
                license=str(record.get("license") or ""),
                license_url=str(record.get("license_url") or ""),
                author=_author(record),
                source_page=str(record.get("source_page") or ""),
                source_title=str(
                    record.get("source_title") or record.get("search_term") or ""
                ),
            )
            registered += 1
            existing = conn.execute(
                "SELECT 1 FROM person_assets WHERE person_id=? AND asset_id=? LIMIT 1",
                (person_id, asset_id),
            ).fetchone()
            person_repo.link_person_asset(
                conn, public_id, asset_id, state="pending"
            )
            if existing is None:
                linked += 1
    finally:
        conn.close()
    logger.info(
        f"material assets registered: task_id={task_id}, person={name!r}, "
        f"registered={registered}, newly_linked={linked}, skipped={skipped}"
    )
    return {"registered": registered, "linked": linked, "skipped": skipped}
