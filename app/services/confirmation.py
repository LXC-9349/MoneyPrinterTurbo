"""分阶段确认（里程碑 4 #5）：确认屏视图 + 分镜编辑 + 检查点续跑入参装配。

任务在“脚本生成后 / 素材获取后”暂停为 ``awaiting_confirmation``（见 ``task.py``
的门槛与检查点）。本模块负责：

* 读回检查点与任务清单，合成确认屏所需的“分镜卡片”视图（逐镜旁白、已选素材、
  候选、来源授权、覆盖率与独立素材数、缺素材/低相关警告）；
* 素材后暂停时的编辑操作（换图 / 从候选选择 / 上传 / 排除 / 删除镜头），全部落在
  检查点的 ``materials`` 列表上，确认后以编辑结果续跑成片阶段；
* 把“确认并继续”翻译成流水线可识别的 ``resume_from`` 参数。

编辑只作用于画面轨（素材列表），不改解说音频/字幕——成片续跑复用已生成的配音与
字幕，因此删除镜头是移除该镜的画面卡而非重切旁白。相关性口径与 M4#2/M4#4 完全一致。
"""
from __future__ import annotations

import os
from typing import Any

from loguru import logger

from app.services import task_artifacts
from app.utils import utils

# 检查点里允许被编辑覆盖的字段（其余为续跑只读产物）。
_EDITABLE_STAGE = "materials"


def read_checkpoint(task_id: str) -> dict:
    """读回确认检查点；缺失/损坏返回空 dict。"""
    ckpt = task_artifacts.read_script_data(task_id).get("confirmation_checkpoint")
    return ckpt if isinstance(ckpt, dict) else {}


def _person_for_task(task_id: str) -> str:
    params = task_artifacts.read_script_data(task_id).get("params") or {}
    if not isinstance(params, dict):
        return ""
    return str(params.get("biography_person") or params.get("video_subject") or "").strip()


def _storyboard_shots(task_id: str) -> list[dict]:
    data = task_artifacts.read_script_data(task_id)
    sb = data.get("storyboard")
    if isinstance(sb, dict):
        for key in ("shots", "timeline", "segments"):
            rows = sb.get(key)
            if isinstance(rows, list) and rows:
                return [r for r in rows if isinstance(r, dict)]
    bio = data.get("biography_script")
    if isinstance(bio, dict) and isinstance(bio.get("timeline"), list):
        return [r for r in bio["timeline"] if isinstance(r, dict)]
    return []


def _material_sources_map(task_id: str) -> dict[str, dict]:
    """把 script.json 的 material_sources 记录按本地文件名归一，供卡片显示授权。"""
    data = task_artifacts.read_script_data(task_id)
    sources = data.get("material_sources")
    mapping: dict[str, dict] = {}
    if isinstance(sources, list):
        for row in sources:
            if not isinstance(row, dict):
                continue
            for k in (row.get("local_file"), row.get("file"), row.get("path")):
                if k:
                    mapping[os.path.basename(str(k))] = row
    return mapping


# 档案（相关）素材与 AI 生成素材的提供方集合，用于把镜头分为
# 相关 / 兜底 / 缺失 三类（批次 B 确认屏三数）。
_ARCHIVE_PROVIDERS = {"wikimedia", "loc"}
_AI_PROVIDERS = {
    "wavespeed", "openai_image", "volcengine_seedance", "ofox", "muapi",
    "metaso_minimax", "loomloom",
}


def _classify_material(chosen: str, src: dict, visual_type: str) -> tuple[str, str]:
    """把一个镜头的已选素材分为 relevant/fallback/missing，并给出兜底原因。

    - 无画面→missing；
    - 来自档案提供方或带结构化相关性依据→relevant；
    - 其余（库存 b-roll / AI 插图）→fallback，不计入“素材齐备”。
    """
    if not chosen:
        return "missing", ""
    provider = str(src.get("provider") or "").lower()
    basis = str(src.get("relevance_basis") or "")
    if provider in _ARCHIVE_PROVIDERS or basis:
        return "relevant", ""
    if provider in _AI_PROVIDERS:
        return "fallback", "AI 插图（未找到相关档案素材）"
    if (visual_type or "").strip().lower() == "b_roll":
        return "fallback", "主题性 b-roll（场景词库存素材）"
    return "fallback", "库存兜底素材（非人物相关档案）"


def _display_author(src: dict) -> str:
    """从素材来源记录取可展示的作者名。

    pexels/wikimedia 的 ``creator``/``author`` 常是嵌套 dict（{id,name,profile_page}），
    直接 ``str(dict)`` 会把原始字典字面量显示到卡片上；这里只抽 ``name``，非 dict
    时按字符串返回，缺失返回空串。
    """
    raw = src.get("author") or src.get("creator")
    if isinstance(raw, dict):
        return str(raw.get("name") or raw.get("display_name") or "")
    return str(raw or "")


def confirmation_view(task_id: str) -> dict:
    """确认屏视图：逐镜卡片 + 覆盖率 + 独立素材数 + 暂停阶段 + 缺素材/低相关提醒。

    候选列表复用 M4#4 分镜预览（不重造检索口径）。预览异常只降级为该镜空候选，
    绝不让确认屏整体失败。
    """
    ckpt = read_checkpoint(task_id)
    stage = str(ckpt.get("stage") or "")
    materials = [str(m) for m in (ckpt.get("materials") or []) if m]
    person = _person_for_task(task_id)
    shots = _storyboard_shots(task_id)
    sources = _material_sources_map(task_id)
    # 素材暂停检查点里的 terms 与镜头 1:1 对齐（见 task.py 门槛）。显式文案任务
    # 没有结构化 storyboard 时，用它兜底逐镜检索词，避免卡片“检索：—”。
    terms = [str(t) for t in (ckpt.get("terms") or [])]

    # 逐镜候选：优先按 storyboard 的 visual_query 复用分镜预览，得到相关性/来源/授权。
    candidate_by_index: dict[int, list[dict]] = {}
    if person and shots:
        try:
            from app.services import preview as preview_service

            pv = preview_service.storyboard_preview(
                person,
                [
                    {
                        "narration": s.get("narration") or s.get("text") or "",
                        "visual_query": s.get("visual_query") or s.get("query") or "",
                        "visual_type": s.get("visual_type") or "historical_photo",
                    }
                    for s in shots
                ],
            )
            for i, shot_view in enumerate(pv.get("shots") or []):
                candidate_by_index[i] = shot_view.get("candidates") or []
        except Exception as exc:  # noqa: BLE001 - 预览降级不影响确认屏
            logger.warning(
                f"confirmation preview failed: task_id={task_id}, "
                f"error={type(exc).__name__}: {exc}"
            )

    cards: list[dict] = []
    n = max(len(shots), len(materials))
    for i in range(n):
        shot = shots[i] if i < len(shots) else {}
        chosen = materials[i] if i < len(materials) else ""
        src = sources.get(os.path.basename(chosen), {}) if chosen else {}
        candidates = candidate_by_index.get(i, [])
        relevant = [c for c in candidates if c.get("passes_gate") and not c.get("excluded")]
        visual_type = str(shot.get("visual_type") or "historical_photo")
        material_class, fallback_reason = _classify_material(chosen, src, visual_type)
        warning = ""
        if material_class == "missing":
            warning = "缺素材：该镜当前无可用画面"
        elif material_class == "fallback":
            # 兜底镜头醒目标记并写明原因，不计入“素材齐备”。
            warning = f"兜底：{fallback_reason}"
        elif stage == _EDITABLE_STAGE and candidates and not relevant and len(candidates) < 8:
            warning = "低相关：候选未通过相关性门槛"
        cards.append(
            {
                "index": i,
                "narration": str(shot.get("narration") or shot.get("text") or ""),
                "query": str(
                    shot.get("visual_query")
                    or shot.get("query")
                    or (terms[i] if i < len(terms) else "")
                ),
                "visual_type": visual_type,
                "archive_subtype": str(src.get("archive_subtype") or ""),
                "material_class": material_class,
                "fallback_reason": fallback_reason,
                "chosen": chosen,
                "chosen_name": os.path.basename(chosen) if chosen else "",
                "license": str(src.get("license") or ""),
                "license_url": str(src.get("license_url") or ""),
                "source_page": str(src.get("source_page") or ""),
                "author": _display_author(src),
                "candidates": candidates[:8],
                "relevant_count": len(relevant),
                "warning": warning,
            }
        )

    independent = len({os.path.basename(m) for m in materials if m})
    covered = sum(1 for c in cards if c["chosen"])
    # 三数指标（批次 B）：相关素材 N / 兜底 K / 缺失 L。兜底不计入“素材齐备”。
    relevant_shots = sum(1 for c in cards if c["material_class"] == "relevant")
    fallback_shots = sum(1 for c in cards if c["material_class"] == "fallback")
    missing_shots_n = sum(1 for c in cards if c["material_class"] == "missing")
    relevant_assets = len({
        c["chosen_name"] for c in cards
        if c["material_class"] == "relevant" and c["chosen_name"]
    })
    data = task_artifacts.read_script_data(task_id)
    return {
        "task_id": task_id,
        "person": person,
        "paused_stage": stage,
        "editable": stage == _EDITABLE_STAGE,
        "shot_count": len(cards),
        "coverage": round(covered / len(cards), 3) if cards else 0.0,
        "independent_assets": independent,
        # 相关/兜底/缺失三数（镜头口径）与相关独立素材数。
        "relevant_shots": relevant_shots,
        "fallback_shots": fallback_shots,
        "missing_shots_count": missing_shots_n,
        "relevant_assets": relevant_assets,
        "script": ckpt.get("script", ""),
        "audio_file": ckpt.get("audio_file", ""),
        "audio_duration": ckpt.get("audio_duration", 0),
        "subtitle_path": ckpt.get("subtitle_path", ""),
        "missing_shots": [
            {"index": c["index"], "query": c["query"], "warning": c["warning"],
             "material_class": c["material_class"]}
            for c in cards
            if c["warning"]
        ],
        "relevance_reminders": data.get("material_relevance_reminders") or {},
        "shortage_reminders": data.get("material_shortage_reminders") or {},
        "shots": cards,
    }


def _persist_checkpoint(task_id: str, ckpt: dict) -> bool:
    return task_artifacts.patch_script_data(
        task_id, confirmation_checkpoint=ckpt
    )


def set_material(task_id: str, index: int, path: str) -> dict:
    """把第 index 镜的画面替换为本地文件（换图/从候选选择/上传后落此）。"""
    ckpt = read_checkpoint(task_id)
    if ckpt.get("stage") != _EDITABLE_STAGE:
        raise ValueError("task is not paused at the materials stage")
    materials = [str(m) for m in (ckpt.get("materials") or [])]
    if index < 0 or index > len(materials):
        raise IndexError("shot index out of range")
    if index == len(materials):
        materials.append(path)
    else:
        materials[index] = path
    ckpt["materials"] = materials
    _persist_checkpoint(task_id, ckpt)
    return {"index": index, "materials": materials}


def exclude_material(task_id: str, index: int, *, person: str = "") -> dict:
    """移除第 index 镜的画面并登记人物级黑名单（person_assets state=excluded）。

    黑名单只对已登记到该人物的素材生效（按本地文件名匹配 asset）；未命中的仅从本
    任务的画面列表移除，不影响人物库其余素材。
    """
    ckpt = read_checkpoint(task_id)
    if ckpt.get("stage") != _EDITABLE_STAGE:
        raise ValueError("task is not paused at the materials stage")
    materials = [str(m) for m in (ckpt.get("materials") or [])]
    if index < 0 or index >= len(materials):
        raise IndexError("shot index out of range")
    removed = materials.pop(index)
    ckpt["materials"] = materials
    _persist_checkpoint(task_id, ckpt)

    blacklisted = False
    name = person or _person_for_task(task_id)
    if name:
        try:
            from app.services import person_repo

            base = os.path.basename(removed).lower()
            for asset in person_repo.list_person_assets(name):
                if str(asset.get("path") or "").lower().endswith(base) or (
                    os.path.basename(str(asset.get("path") or "")).lower() == base
                ):
                    person_repo.set_person_asset_state(
                        name, int(asset["asset_id"]), "excluded"
                    )
                    blacklisted = True
                    break
        except Exception as exc:  # noqa: BLE001 - 黑名单登记失败不阻断编辑
            logger.warning(
                f"failed to blacklist excluded asset: task_id={task_id}, "
                f"error={type(exc).__name__}: {exc}"
            )
    return {"index": index, "removed": removed, "blacklisted": blacklisted,
            "materials": materials}


def delete_shot(task_id: str, index: int) -> dict:
    """删除镜头：移除该镜画面卡（画面轨）。旁白音频/字幕按续跑复用的既定语义保留。"""
    return exclude_material(task_id, index)


def download_candidate(task_id: str, url: str) -> str:
    """把一个候选下载进任务目录，返回本地路径（供“从候选选择/换图”调用）。"""
    url = str(url or "").strip()
    if not url:
        raise ValueError("candidate url is required")
    from app.services import material

    save_dir = utils.task_dir(task_id)
    if url.lower().split("?")[0].endswith((".mp4", ".mov", ".webm", ".mkv")):
        local = material.save_video(url, save_dir=save_dir)
    else:
        local = material.save_image(url, save_dir=save_dir)
    if not local:
        raise RuntimeError("failed to download the selected candidate")
    return local


def build_resume_params(task_id: str, resume_from: str) -> dict:
    """把检查点装配成续跑所需的 params 覆盖（供确认端点并入原始任务参数）。

    ``resume_from`` ∈ {"terms","video"}：脚本已确认从关键词续跑、素材已确认从成片
    续跑。这里只回传续跑标记与检查点摘要，真实参数仍由控制器读取登记行后合并。
    """
    if resume_from not in ("terms", "video"):
        raise ValueError(f"invalid resume_from: {resume_from}")
    ckpt = read_checkpoint(task_id)
    if not ckpt:
        raise ValueError("task has no confirmation checkpoint to resume from")
    return {
        "resume_from": resume_from,
        "checkpoint_stage": ckpt.get("stage"),
        "has_script": bool(ckpt.get("script")),
        "materials": list(ckpt.get("materials") or []) if resume_from == "video" else [],
    }


def next_resume_from(stage: str) -> str:
    """确认某个暂停阶段后应续跑的 resume_from 取值。"""
    return {"script": "terms", "materials": "video"}.get(stage, "terms")
