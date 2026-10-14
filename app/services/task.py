import json
import math
import os
import re
import socket
import tempfile
import threading
import time
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from functools import partial
from os import path
from uuid import uuid4

from loguru import logger

from app.config import config
from app.models import const
from app.models.schema import VideoConcatMode, VideoParams
from app.services import bgm as bgm_service
from app.services import (
    elevenlabs_music,
    llm,
    loomloom,
    material,
    metaso_minimax,
    muapi,
    ofox,
    sonilo,
    subtitle,
    task_artifacts,
    twelvelabs,
    video,
    volcengine_seedance,
    voice,
)
from app.services import upload_post
from app.services import state as sm
from app.utils import file_security, utils
from app.utils.subtitle_writer import staged_subtitle_file


# 发布请求最长可等待数分钟，不能继续占用视频生成任务的并发名额。
# 固定大小的线程池将发布吞吐限制在可控范围内，同时让视频产物生成后
# 立即进入完成状态。
_cross_post_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="mpt-cross-post",
)
_cross_post_max_pending_tasks = max(
    1,
    int(config.app.get("upload_post_max_pending_tasks", 10)),
)
_cross_post_slots = threading.BoundedSemaphore(_cross_post_max_pending_tasks)
_cross_post_registry_lock = threading.RLock()
_cross_post_futures: dict[str, Future] = {}
_cross_post_process_owner = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex}"
_ACTIVE_CROSS_POST_STATES = {
    const.CROSS_POST_STATE_PENDING,
    const.CROSS_POST_STATE_PROCESSING,
}
_CROSS_POST_STATE_WRITE_ATTEMPTS = 3
_CROSS_POST_STATE_RETRY_DELAY_SECONDS = 0.1
_TASK_FAILURE_STATE_WRITE_ATTEMPTS = 3
_TASK_FAILURE_STATE_RETRY_DELAY_SECONDS = 0.1
_LOOMLOOM_STATE_WRITE_ATTEMPTS = 3
_LOOMLOOM_STATE_RETRY_DELAY_SECONDS = 0.1
_INTERRUPTED_CROSS_POST_ERROR = (
    "cross-posting was interrupted before the process completed"
)
# Map upload-post platform ids to the social platform names llm.py accepts.
_CROSS_POST_SOCIAL_PLATFORMS = {
    "tiktok": "tiktok",
    "instagram": "instagram_reels",
    "facebook": "facebook_reels",
}
# 视频配乐服务只需实现 ``is_enabled`` 和 ``generate_bgm``。供应商差异集中在
# 文件扩展名、领域异常和 WebUI 警告代码；任务编排、0 音量短路及失败降级
# 全部复用同一路径，避免后续新增供应商时维护多份相似流程。
_VIDEO_MUSIC_PROVIDERS = {
    "sonilo": {
        "service": sonilo,
        "error_type": sonilo.SoniloError,
        "suffix": ".m4a",
        "warning_code": "sonilo_bgm_failed",
        "display_name": "Sonilo",
    },
    "elevenlabs": {
        "service": elevenlabs_music,
        "error_type": elevenlabs_music.ElevenLabsMusicError,
        "suffix": ".mp3",
        "warning_code": "elevenlabs_bgm_failed",
        "display_name": "ElevenLabs",
    },
}


_SUPPORTED_VIDEO_SOURCES = frozenset({
    "pexels", "pixabay", "coverr", "local", "wavespeed",
    "volcengine_seedance", "ofox", "metaso_minimax", "muapi",
    "loomloom", "openai_image",
})


def _get_video_music_prompt(params: VideoParams) -> str:
    """
    读取当前视频配乐供应商实际使用的提示词。

    新任务统一使用供应商无关字段；旧 Sonilo CLI 参数和历史任务仍可能只有
    ``sonilo_bgm_prompt``，因此仅在 Sonilo 通用字段为空时读取旧字段。
    """
    prompt = str(params.video_music_prompt or "").strip()
    if params.bgm_type == "sonilo" and not prompt:
        prompt = str(params.sonilo_bgm_prompt or "").strip()
    return prompt


def is_task_busy(task: dict | None) -> bool:
    """判断任务是否仍在生成或发布，供所有删除入口复用。"""
    if not task:
        return False

    state = task.get("state")
    try:
        state = int(state)
    except (TypeError, ValueError):
        pass

    # 视频生成和跨平台发布都可能继续读取任务目录。统一视为忙碌状态，
    # 可以避免 API 与 WebUI 分别维护规则后出现一个允许删除、另一个禁止
    # 删除的不一致行为。
    return (
        state == const.TASK_STATE_PROCESSING
        or task.get("cross_post_state") in _ACTIVE_CROSS_POST_STATES
    )


def _register_cross_post_future(task_id: str, future: Future) -> None:
    """登记当前进程持有的发布 Future，供启动恢复和测试判断真实运行状态。"""
    with _cross_post_registry_lock:
        _cross_post_futures[task_id] = future


def _unregister_cross_post_future(task_id: str, future: Future | None = None) -> None:
    """仅移除匹配的 Future，避免旧回调误删同任务后续注册的新工作。"""
    with _cross_post_registry_lock:
        current = _cross_post_futures.get(task_id)
        if current is None or (future is not None and current is not future):
            return
        _cross_post_futures.pop(task_id, None)


def _is_cross_post_active_in_process(task_id: str) -> bool:
    """判断当前进程是否仍持有未结束的发布任务。"""
    with _cross_post_registry_lock:
        future = _cross_post_futures.get(task_id)
        return future is not None and not future.done()


def _is_windows_process_alive(process_id: int) -> bool:
    """通过只读 Win32 API 判断进程状态，避免用 os.kill 误终止进程。"""
    import ctypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5
    error_invalid_parameter = 87
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # ctypes 默认把未声明的返回值当作 32 位 int。Windows 64 位进程句柄可能
    # 因此被截断，必须显式声明 Win32 函数签名后再调用。
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        process_id,
    )
    if not handle:
        error_code = ctypes.get_last_error()
        if error_code == error_invalid_parameter:
            return False
        if error_code == error_access_denied:
            # 进程存在但当前用户无查询权限时，必须保守地视为存活，避免错误
            # 回收其它账户正在执行的发布任务。
            return True
        logger.warning(
            "failed to open cross-post owner process on Windows, "
            f"process_id: {process_id}, error_code: {error_code}"
        )
        return True

    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            error_code = ctypes.get_last_error()
            logger.warning(
                "failed to read cross-post owner process state on Windows, "
                f"process_id: {process_id}, error_code: {error_code}"
            )
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _is_cross_post_owner_alive(owner: str | None) -> bool:
    """判断持久化发布任务的本机进程是否仍存在。"""
    if not owner:
        return False

    try:
        hostname, process_id_text, _ = owner.split(":", 2)
        process_id = int(process_id_text)
    except (TypeError, ValueError):
        logger.warning(f"invalid cross-post owner metadata: {owner}")
        return False

    # 无法可靠探测其它主机上的进程。共享 Redis 的多主机部署中必须保守地
    # 视为仍在运行，避免当前节点误删另一节点正在读取的视频文件。
    if hostname != socket.gethostname():
        return True

    # 当前进程内是否仍有真实发布工作，已经由 Future 注册表准确判断。运行到
    # 这里说明注册表中没有对应 Future，即使 owner 与当前进程完全一致，也应
    # 视为已中断；这可以覆盖终态写入持续失败、Future 已结束的场景。
    if process_id == os.getpid():
        return False

    # Windows 的 os.kill(pid, 0) 与 POSIX 语义不同，可能直接终止目标进程。
    # 使用只申请查询权限的 Win32 API，不向目标进程发送任何信号。
    if os.name == "nt":
        return _is_windows_process_alive(process_id)

    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        logger.warning(
            f"failed to inspect cross-post owner process, owner: {owner}, error: {exc}"
        )
        return True
    return True


def _mark_task_failed(
    task_id: str,
    stage: str,
    error: str,
    details: dict | None = None,
) -> dict:
    """记录结构化失败信息，并保留任务失败前已经到达的进度。"""
    existing_task = None
    try:
        existing_task = sm.state.get_task(task_id)
    except Exception as exc:
        logger.warning(f"failed to read task state before failure update: {exc}")

    # 具体服务函数通常比编排层拥有更准确的错误原因。后续的空结果检查
    # 不能再用通用文案覆盖它，否则 API 调用方仍然只能看到模糊信息。
    if (
        existing_task
        and existing_task.get("state") == const.TASK_STATE_FAILED
        and existing_task.get("error")
    ):
        return existing_task

    message = str(error or "unknown task error").strip()
    progress = int((existing_task or {}).get("progress", 0) or 0)
    logger.error(f"task failed, task_id: {task_id}, stage: {stage}, error: {message}")
    failure = {
        "task_id": task_id,
        "state": const.TASK_STATE_FAILED,
        "progress": progress,
        "failed_stage": stage,
        "error": message,
    }
    # 某些外部任务已经创建了可用于恢复或排障的远端 ID。失败状态需要保留
    # 这些非敏感字段，但不能允许调用方覆盖统一的状态、进度和错误结构。
    # Keep completed batch outputs in both the persisted state and the direct
    # return value used by the CLI. These fields are reset before a new attempt.
    failure_details = {
        key: existing_task[key]
        for key in ("videos", "combined_videos", "warnings", "failed_video_index")
        if existing_task and key in existing_task
    }
    failure_details.update({
        key: value for key, value in dict(details or {}).items() if key not in failure
    })
    failure.update(failure_details)
    fields = {key: value for key, value in failure.items() if key != "task_id"}
    for attempt in range(_TASK_FAILURE_STATE_WRITE_ATTEMPTS):
        try:
            if attempt == 0:
                sm.state.update_task(task_id, **fields)
            else:
                # A lost acknowledgement may mean the first write succeeded.
                # Patch only an existing record so retries cannot revive a task
                # that the user has since deleted.
                sm.state.patch_task(task_id, **fields)
            break
        except Exception as exc:
            if attempt + 1 == _TASK_FAILURE_STATE_WRITE_ATTEMPTS:
                logger.exception(
                    f"failed to persist task failure, task_id: {task_id}, error: {exc}"
                )
            else:
                time.sleep(_TASK_FAILURE_STATE_RETRY_DELAY_SECONDS)
    # A state outage must not discard the only completed-file snapshot held by
    # the caller. Return it even when all bounded persistence attempts fail.
    return failure


def generate_biography_script(task_id, params):
    """Structured biography script (phase 3).

    Returns (narration_text, script_dict) or (None, None). The narration text
    feeds the existing TTS/subtitle pipeline unchanged; the script dict drives
    per-shot material routing via ``shot_visual_types``.
    """
    person = (getattr(params, "biography_person", "") or "").strip()
    if not person:
        person = (params.video_subject or "").strip()
    if not person:
        return None, None

    # 用户勾选的"基础脚本"文章(传记向导步②)。清洗为 [str, str] 去掉空白/空项,
    # 直接透传给 biography.generate_structured_script(base_doc_ids=...)。
    # 该键恒传入：未选时传 [] 即保持既有自动挖掘路径。
    base_doc_ids = [
        str(d).strip()
        for d in (getattr(params, "biography_doc_ids", []) or [])
        if str(d).strip()
    ]

    script = params.biography_script if isinstance(params.biography_script, dict) else None
    if not script:
        from app.services import biography

        target = _biography_target_seconds(params)
        script = biography.generate_structured_script(
            person,
            language=params.video_language or "zh-CN",
            target_seconds=target,
            shot_count=max(6, min(20, target // 16)),
            base_doc_ids=base_doc_ids,
        )
    if not script:
        return None, None

    from app.services import biography

    narration = biography.script_to_narration(script)
    if not narration:
        return None, None

    # Persist the structured script + fact-check flags for human review.
    try:
        task_artifacts.patch_script_data(
            task_id,
            biography_script=script,
            biography_facts_check=script.get("facts_check"),
        )
    except Exception as exc:
        logger.warning(
            f"failed to persist biography script: {type(exc).__name__}: {exc}"
        )

    logger.success(
        f"biography script: person={person}, shots={len(script.get('timeline') or [])}, "
        f"narration_chars={len(narration)}"
    )
    return narration, script


def _biography_target_seconds(params) -> int:
    """Target narration length: explicit duration_hint sum, else 4 minutes."""
    script = getattr(params, "biography_script", None)
    if isinstance(script, dict):
        total = sum(
            int(shot.get("duration_hint") or 0)
            for shot in (script.get("timeline") or [])
            if isinstance(shot, dict)
        )
        if total > 0:
            return total
    explicit = config.app.get("biography_target_seconds", 0)
    try:
        explicit = int(explicit)
    except (TypeError, ValueError):
        explicit = 0
    return explicit if explicit > 0 else 240


def _stage_progress_reporter(task_id: str, stage_start: float, stage_end: float):
    """
    返回一个回调，把阶段内 0~1 的完成比例换算成任务进度并写入状态。

    流水线原先只在阶段边界更新进度，下载素材和处理片段这类耗时阶段期间
    进度条完全不动，用户无法区分仍在运行与已经卡死。进度只是展示信息：
    比例无法解析时忽略，状态后端写入失败时只记录日志，不能因此中断下载或
    合成。
    """

    def report(fraction) -> None:
        try:
            fraction = max(0.0, min(1.0, float(fraction)))
        except (TypeError, ValueError):
            return
        try:
            sm.state.update_task(
                task_id,
                state=const.TASK_STATE_PROCESSING,
                progress=stage_start + (stage_end - stage_start) * fraction,
            )
        except Exception as exc:
            logger.warning(
                f"failed to update task progress: task_id={task_id}, "
                f"error={type(exc).__name__}, detail={exc}"
            )

    return report


def generate_script(task_id, params):
    logger.info("\n\n## generating video script")
    video_script = params.video_script.strip()
    # 用户显式提供了口播稿(而非系统生成)时,后续内容预检只留痕不改写:
    # 尊重其确切意图,避免把用户逐字写好的解说悄悄替换成模型重写的内容。
    explicit_script = bool(video_script)

    # Biography fork: when a subject person is set (or the archive source is
    # used), build the structured script first so shots carry visual_type and
    # the material dispatcher knows which shots need real historical photos.
    if params.video_source == "historical_archive" or getattr(
        params, "biography_person", ""
    ):
        narration, script = generate_biography_script(task_id, params)
        if script and narration and not video_script:
            params.biography_script = script
            video_script = narration
        elif not video_script and not narration:
            logger.warning(
                "biography script generation failed, falling back to the stock "
                "script prompt"
            )

    if not video_script:
        # 普通视频:优先走结构化分镜,使画面-解说按镜对齐、时长有真实起伏。
        # 传记模式已在上方用历史档案分镜处理;这里只处理普通素材源。
        # 用户给出了自定义风格/系统提示词时尊重其控制,不再自动分镜(避免覆盖
        # 用户的显式意图,也让"编排层原样透传 prompt 选项"的契约保持成立)。
        user_gave_prompt = bool(
            str(getattr(params, "video_script_prompt", "") or "").strip()
            or str(getattr(params, "custom_system_prompt", "") or "").strip()
        )
        storyboard_enabled = (
            bool(config.app.get("storyboard_enabled", True))
            and getattr(params, "video_source", "") not in ("historical_archive",)
            and not getattr(params, "biography_person", "")
            and not user_gave_prompt
        )
        if storyboard_enabled:
            try:
                from app.services import storyboard as storyboard_service

                target_seconds = int(
                    os.environ.get("MPT_STORYBOARD_SECONDS", "45") or 45
                )
                shot_count = max(3, min(int(params.paragraph_number or 1) * 6, 8))
                sb = storyboard_service.generate_storyboard(
                    params.video_subject,
                    language=params.video_language,
                    target_seconds=target_seconds,
                    shot_count=shot_count,
                )
                if sb:
                    # VideoParams 是 extra=forbid 的严格请求契约,不能新增字段。
                    # 分镜暂存到任务目录下的独立 storyboard.json(generate_terms 读它,
                    # save_script_data 之后再把结构合并进 script.json 供作品追溯)。
                    video_script = storyboard_service.storyboard_to_script(sb)
                    _stage_storyboard(task_id, sb)
                    logger.info(
                        f"storyboard script ready: {storyboard_service.storyboard_shot_count(sb)} shots"
                    )
            except Exception as exc:
                logger.warning(
                    f"storyboard generation failed, falling back to flat script: "
                    f"{type(exc).__name__}: {exc}"
                )

    if not video_script:
        video_script = llm.generate_script(
            video_subject=params.video_subject,
            language=params.video_language,
            paragraph_number=params.paragraph_number,
            video_script_prompt=getattr(params, "video_script_prompt", ""),
            custom_system_prompt=getattr(params, "custom_system_prompt", ""),
        )
    else:
        logger.debug(f"video script: \n{video_script}")

    if not video_script:
        _mark_task_failed(task_id, "script", "failed to generate video script")
        return None

    video_script = _content_gate_and_retry(
        task_id, params, video_script, explicit_script=explicit_script
    )
    return video_script


def _content_target_seconds(params) -> int:
    """本次脚本的口播时长目标(秒)。0 表示不按目标判定。"""
    try:
        if params.video_source == "historical_archive" or getattr(
            params, "biography_person", ""
        ):
            return _biography_target_seconds(params)
        return int(os.environ.get("MPT_STORYBOARD_SECONDS", "45") or 45)
    except Exception:
        return 0


def _content_gate_and_retry(task_id, params, video_script, explicit_script=False):
    """内容质量预检 + 不达标时的一次自动重试(生产级内容保障)。

    规则(全部可配置):
    - 首轮用确定性检查(zero-LLM)判定内容问题;
    - 若仅因"偏短"未过,且允许重试,则用更长的目标重生成一次脚本
      (只重试一次,避免无限循环与额外成本);重试结果仍不合格则保留并留痕。
    - 结论始终写入 script.json.content_report,不阻断交付。
    - explicit_script=True 时只做预检留痕、不自动重写:用户逐字提供的口播稿
      是其确切意图,不应被模型改写。
    """
    try:
        from app.services import content_check
    except Exception:
        return video_script

    target = _content_target_seconds(params)
    report = content_check.check_script(video_script, target_seconds=target)

    # 自动重试:仅在"偏短"、允许重试、未给时长目标以外且未用用户显式
    # 提示词时,要求模型写更长;只重试一次,避免无限循环与额外成本。
    # 用户显式给了 video_script_prompt/custom_system_prompt 时不重写:尊重其
    # 意图(可能故意要短文,或提示里已指定风格),避免注入"[要求]写更长"
    # 覆盖用户的显式要求。
    allow_retry = bool(config.app.get("content_autoretry", True))
    user_gave_prompt = bool(
        str(getattr(params, "video_script_prompt", "") or "").strip()
        or str(getattr(params, "custom_system_prompt", "") or "").strip()
    )
    only_short = report.get("issues") and set(report.get("issues")) <= {"too_short"}
    if (
        not report.get("passed")
        and only_short
        and allow_retry
        and target > 0
        and not user_gave_prompt
        and not explicit_script
    ):
        try:
            longer_prompt = (
                (getattr(params, "video_script_prompt", "") or "")
                + f"\n\n[要求] 请把口播稿写到约 {target} 秒(约 {int(target * 5)} 个汉字),"
                "信息密度要高,不要用空泛的套话。"
            )
            retry_script = llm.generate_script(
                video_subject=params.video_subject,
                language=params.video_language,
                paragraph_number=params.paragraph_number,
                video_script_prompt=longer_prompt,
                custom_system_prompt=getattr(params, "custom_system_prompt", ""),
            )
            if retry_script and retry_script.strip():
                retry_report = content_check.check_script(
                    retry_script, target_seconds=target
                )
                # 只有重试更好(问题更少)才采用,避免越改越差。
                if len(retry_report.get("issues") or []) < len(
                    report.get("issues") or []
                ):
                    logger.info(
                        "content autoretry improved script: "
                        f"{report.get('issues')} -> {retry_report.get('issues')}"
                    )
                    video_script = retry_script
                    report = retry_report
        except Exception as exc:
            logger.warning(f"content autoretry failed: {type(exc).__name__}: {exc}")

    if not report.get("passed"):
        logger.warning(f"content check flagged script: {report.get('issues')}")
    try:
        task_artifacts.patch_script_data(task_id, content_report=report)
    except Exception as exc:
        logger.warning(f"failed to persist content report: {type(exc).__name__}")
    return video_script


def generate_terms(task_id, params, video_script):
    logger.info("\n\n## generating video terms")
    video_terms = params.video_terms

    # Biography fork: the structured script already carries a per-shot archive
    # query (visual_query). Reusing it keeps material order aligned with the
    # narration and avoids a second LLM call.
    if not video_terms:
        script = getattr(params, "biography_script", None)
        if isinstance(script, dict):
            from app.services import biography

            script_terms = biography.script_to_search_terms(script)
            if script_terms:
                logger.info(
                    f"using {len(script_terms)} visual queries from the biography script"
                )
                return script_terms

    if not video_terms:
        # 普通视频分镜脚本已带逐镜画面检索词(由 generate_script 落到 script.json),
        # 直接复用,顺序与解说 1:1 对齐,并省去一次 LLM 调用。
        # 传记模式的分镜在上方已先行处理。
        sb = _load_staged_storyboard(task_id)
        if isinstance(sb, dict):
            from app.services import storyboard as storyboard_service

            sb_terms = storyboard_service.storyboard_search_terms(sb)
            if sb_terms:
                logger.info(
                    f"using {len(sb_terms)} visual queries from the storyboard"
                )
                return sb_terms

        # 开启素材按文案顺序匹配后,关键词本身也必须按脚本叙事顺序生成;
        # 否则后续即使顺序下载和顺序拼接,也只能复用一组全局主题词,
        # 无法改善“后面内容的画面提前出现”的问题。
        video_terms = llm.generate_terms(
            video_subject=params.video_subject,
            video_script=utils.remove_pause_tags(video_script),
            amount=8 if params.match_materials_to_script else 5,
            match_script_order=params.match_materials_to_script,
        )
    else:
        if isinstance(video_terms, str):
            video_terms = [term.strip() for term in re.split(r"[,，]", video_terms)]
        elif isinstance(video_terms, list):
            video_terms = [term.strip() for term in video_terms]
        else:
            raise ValueError("video_terms must be a string or a list of strings.")

        # Delimiter-only input must not reach paid reranking or material search.
        video_terms = [term for term in video_terms if term]

        logger.debug(f"video terms: {utils.to_json(video_terms)}")


    if not video_terms:
        _mark_task_failed(
            task_id,
            "terms",
            "failed to generate video search terms",
        )
        return None

    # 可选的 TwelveLabs Marengo 语义重排：未启用时返回原顺序，无任何副作用。
    # 顺序匹配模式下关键词顺序本身就是脚本叙事顺序，必须保持原样，故跳过。
    if not params.match_materials_to_script:
        video_terms = twelvelabs.rerank_terms_by_subject(
            video_subject=params.video_subject,
            search_terms=video_terms,
        )

    return video_terms


def save_script_data(task_id, video_script, video_terms, params):
    script_data = {
        "script": video_script,
        "search_terms": video_terms,
        "params": params,
    }
    task_artifacts.write_script_data(task_id, script_data)


def resolve_custom_audio_file(
    task_id: str,
    custom_audio_file: str | None,
    *,
    allow_server_file_input: bool = False,
) -> str:
    requested_file = (custom_audio_file or "").strip()
    if not requested_file:
        return ""

    task_dir = utils.task_dir(task_id)
    try:
        return file_security.resolve_path_within_directory(
            task_dir,
            requested_file,
        )
    except ValueError as exc:
        task_dir_error = exc

    # A missing path that otherwise stays inside the task directory is safe to
    # report precisely. Paths outside that boundary use the same generic error
    # regardless of whether they exist, so callers cannot probe the host filesystem.
    if str(task_dir_error) == "file does not exist":
        raise task_dir_error

    # HTTP requests and other untrusted callers must never turn a submitted path
    # into a server-side file read. WebUI uploads already live in the task directory;
    # only the local CLI explicitly opts into resolving files elsewhere on the host.
    if not allow_server_file_input:
        raise ValueError(
            "custom audio file must be stored within the current task directory"
        ) from task_dir_error

    server_audio_file = path.realpath(
        requested_file
        if path.isabs(requested_file)
        else path.join(utils.root_dir(), requested_file)
    )
    if not path.isabs(requested_file):
        project_root = path.realpath(utils.root_dir())
        try:
            if path.commonpath([project_root, server_audio_file]) != project_root:
                raise ValueError(
                    "relative custom audio paths must stay within the project directory"
                )
        except ValueError as exc:
            raise ValueError(
                "custom audio file must be task-local or an existing server-side file"
            ) from exc

    if not path.isfile(server_audio_file):
        raise ValueError(
            "custom audio file does not exist or is not a file"
        ) from task_dir_error

    return server_audio_file


def _resolve_reusable_voice_preview(
    task_id: str,
    params,
    video_script: str,
    voice_preview: dict | None,
) -> tuple[str, float, object] | None:
    """
    校验并解析 WebUI 提交的完整试听缓存。

    该载荷不是公开 API 参数，只能来自当前进程的 WebUI。即便如此，后台任务
    仍重新核对文案和全部配音参数，并限制音频位于当前任务目录；任何不一致都
    回退普通 TTS，不让过期试听污染正式成片。
    """
    if not voice_preview:
        return None

    expected_values = {
        "script": str(video_script or "").strip(),
        "voice_name": params.voice_name,
        "voice_rate": float(params.voice_rate),
        "voice_volume": float(params.voice_volume),
    }
    if not math.isclose(float(params.voice_volume), 1.0) or any(
        voice_preview.get(key) != value for key, value in expected_values.items()
    ):
        logger.info(
            f"skip stale voice preview cache, task_id: {task_id}, "
            "reason: voice parameters changed"
        )
        return None

    preview_file = path.realpath(str(voice_preview.get("audio_file") or ""))
    task_root = path.realpath(utils.task_dir(task_id))
    try:
        preview_is_task_local = path.commonpath([task_root, preview_file]) == task_root
    except ValueError:
        preview_is_task_local = False

    duration = voice_preview.get("duration")
    sub_maker = voice_preview.get("sub_maker")
    if (
        not preview_is_task_local
        or not path.isfile(preview_file)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration <= 0
        or sub_maker is None
    ):
        logger.warning(
            f"skip invalid voice preview cache, task_id: {task_id}, "
            f"audio_file: {preview_file or '<empty>'}"
        )
        return None

    logger.info(
        f"using full voice preview audio, task_id: {task_id}, duration: {duration:.2f}s"
    )
    return preview_file, math.ceil(duration), sub_maker


def _try_shot_audio(task_id, params):
    """Per-shot narration audio for a biography task, or None to skip.

    Only used when a structured script exists (its timeline defines the shots).
    Any failure returns None so the caller falls back to single-pass TTS.
    """
    script = getattr(params, "biography_script", None)
    if not isinstance(script, dict):
        return None
    shots = [s for s in (script.get("timeline") or []) if isinstance(s, dict)]
    if len(shots) < 2:
        return None

    from app.services import shot_audio

    try:
        result = shot_audio.synthesize_shots(
            shots,
            voice_name=voice.parse_voice_name(params.voice_name),
            voice_rate=params.voice_rate,
            task_dir=utils.task_dir(task_id),
            voice_volume=params.voice_volume,
        )
    except Exception as exc:
        logger.warning(
            f"per-shot audio unavailable, using single-pass TTS: "
            f"{type(exc).__name__}: {exc}"
        )
        return None
    audio_path, total, timings, sub_maker = result
    if not audio_path or not timings:
        return None
    logger.info(
        f"per-shot audio ready: {len(timings)} shots, total {total:.1f}s"
    )
    return audio_path, total, timings, sub_maker


def generate_audio(
    task_id,
    params,
    video_script,
    voice_preview=None,
    *,
    allow_server_file_input: bool = False,
    voxcpm_reference_audio: bytes | None = None,
    voxcpm_prompt_audio: bytes | None = None,
    voxcpm_prompt_text: str = "",
):
    """
    Generate audio for the video script.
    If a custom audio file is provided, it will be used directly.
    There will be no subtitle maker object returned in this case.
    Otherwise, TTS will be used to generate the audio.
    Returns:
        - audio_file: path to the generated or provided audio file
        - audio_duration: duration of the audio in seconds
        - sub_maker: subtitle maker object if TTS is used, None otherwise
    """
    logger.info("\n\n## generating audio")
    # /audio 和 /subtitle 请求模型不包含 custom_audio_file，
    # 这里统一做兼容读取，避免直调接口时抛属性错误。
    requested_custom_audio_file = getattr(params, "custom_audio_file", None)
    try:
        custom_audio_file = resolve_custom_audio_file(
            task_id,
            requested_custom_audio_file,
            allow_server_file_input=allow_server_file_input,
        )
    except ValueError as exc:
        _mark_task_failed(
            task_id,
            "audio",
            f"invalid custom audio file: {exc}",
        )
        return None, None, None

    if not custom_audio_file:
        reusable_preview = _resolve_reusable_voice_preview(
            task_id,
            params,
            video_script,
            voice_preview,
        )
        if reusable_preview:
            return reusable_preview

        logger.info("no custom audio file provided, using TTS to generate audio.")
        audio_file = path.join(utils.task_dir(task_id), "audio.mp3")
        # tts_provider seam (phase 4): MVP resolves to Edge TTS; a CosyVoice2
        # cloned voice can be plugged in via config without touching callers.
        from app.services import tts_provider

        # Biography fork: when a structured script exists, synthesize shot by
        # shot. The cumulative segment durations give exact shot boundaries,
        # which keeps subtitles and visuals locked to the narration instead of
        # relying on estimation. Falls back to the single-pass path below.
        sub_maker = None
        shot_audio_result = _try_shot_audio(task_id, params)
        if shot_audio_result:
            shot_audio_path, shot_total, shot_timings, shot_sub = shot_audio_result
            audio_file = shot_audio_path
            sub_maker = shot_sub
            try:
                task_artifacts.patch_script_data(
                    task_id,
                    shot_timings=[
                        {"start": round(s, 3), "end": round(e, 3), "text": t}
                        for s, e, t in shot_timings
                    ],
                )
            except Exception as exc:
                logger.warning(
                    f"failed to persist shot timings: {type(exc).__name__}"
                )

        if sub_maker is None:
            # NOTE: voice_volume is deliberately NOT forwarded here. The video
            # compositing stage applies the volume gain, and passing it to TTS
            # as well would double-apply it (see
            # test_non_default_volume_regenerates_audio_without_double_gain).
            tts_kwargs = {
                "text": video_script,
                "voice_name": voice.parse_voice_name(params.voice_name),
                "voice_rate": params.voice_rate,
                "voice_file": audio_file,
            }
            if voxcpm_reference_audio is not None:
                tts_kwargs["voxcpm_reference_audio"] = voxcpm_reference_audio
            if voxcpm_prompt_audio is not None:
                tts_kwargs["voxcpm_prompt_audio"] = voxcpm_prompt_audio
                tts_kwargs["voxcpm_prompt_text"] = voxcpm_prompt_text
            sub_maker = tts_provider.synthesize(**tts_kwargs)
        if sub_maker is None:
            _mark_task_failed(
                task_id,
                "audio",
                "failed to synthesize audio; verify the selected voice and TTS connectivity",
            )
            return None, None, None
        # Measure the real written audio_file, not sub_maker.cues[-1].end:
        # the latter is the last WORD BOUNDARY, and TTS leaves a fixed tail
        # past it (Edge TTS: ~0.88s at any length - 19% of a 7-word clip but
        # 1.4% of a 153-word one, so short scripts suffer most). The
        # under-count sizes paid generate_bgm() calls, is reported as
        # audio_duration to the API/WebUI, and under-sources
        # download_videos() material, scaled by video_count.
        file_duration = voice.get_audio_duration(audio_file)
        audio_duration = math.ceil(
            file_duration if file_duration > 0 else voice.get_audio_duration(sub_maker)
        )
        if audio_duration == 0:
            _mark_task_failed(task_id, "audio", "generated audio duration is zero")
            return None, None, None
        return audio_file, audio_duration, sub_maker
    else:
        logger.info(f"using custom audio file: {custom_audio_file}")
        audio_duration = voice.get_audio_duration(custom_audio_file)
        if audio_duration == 0:
            _mark_task_failed(
                task_id,
                "audio",
                "custom audio duration is zero",
            )
            return None, None, None
        return custom_audio_file, audio_duration, None


def _shot_timings_from_audio_dir(task_id, shots, audio_file):
    """Recompute exact shot timings from the per-shot audio segments.

    Reading the segment files (rather than trusting in-memory state) keeps this
    correct on the `stop_at=audio` / resumed-task paths, where generate_audio
    ran in a different call. Returns None when the per-shot directory is absent,
    which signals "use the normal subtitle provider" to the caller.
    """
    import os

    segments_dir = path.join(utils.task_dir(task_id), "audio_shots")
    if not os.path.isdir(segments_dir):
        return None
    texts = [str(s.get("narration") or "").strip() for s in shots]
    texts = [t for t in texts if t]
    segment_files = sorted(
        f for f in os.listdir(segments_dir)
        if f.startswith("shot-") and f.endswith(".mp3")
    )
    if len(segment_files) != len(texts):
        return None

    from app.services import shot_audio

    durations = []
    for name in segment_files:
        duration = shot_audio._probe_duration(
            path.join(segments_dir, name), _audio_clip_cls()
        )
        if duration <= 0:
            return None
        durations.append(duration)

    pause = shot_audio._INTER_SHOT_SILENCE_MS / 1000.0
    timings = []
    cursor = 0.0
    for idx, (text, duration) in enumerate(zip(texts, durations)):
        timings.append((cursor, cursor + duration, text))
        cursor += duration
        if idx < len(durations) - 1:
            cursor += pause
    return timings


def _audio_clip_cls():
    from moviepy import AudioFileClip

    return AudioFileClip


def generate_subtitle(task_id, params, video_script, sub_maker, audio_file):
    """
    Generate subtitle for the video script.
    If subtitle generation is disabled or no subtitle maker is provided, it will return an empty string.
    Otherwise, it will generate the subtitle using the specified provider.
    Returns:
        - subtitle_path: path to the generated subtitle file
    """
    logger.info("\n\n## generating subtitle")
    if not params.subtitle_enabled:
        return ""

    final_subtitle_path = path.join(utils.task_dir(task_id), "subtitle.srt")
    subtitle_provider = config.app.get("subtitle_provider", "edge").strip().lower()
    logger.info(f"\n\n## generating subtitle, provider: {subtitle_provider}")

    if not subtitle_provider:
        logger.info("subtitle provider is empty, skip subtitle generation")
        return ""

    # Biography fork: per-shot TTS produced exact shot boundaries. Using them
    # as the subtitle timeline is strictly better than whisper re-alignment
    # (no model download, no ASR drift) while staying provider-independent.
    script = getattr(params, "biography_script", None)
    if isinstance(script, dict) and script.get("timeline"):
        from app.services import shot_audio

        shots = [s for s in script["timeline"] if isinstance(s, dict)]
        timings = _shot_timings_from_audio_dir(task_id, shots, audio_file)
        if timings:
            try:
                with open(subtitle_path, "w", encoding="utf-8") as f:
                    f.write(shot_audio.timings_to_srt(timings))
                logger.success(
                    f"subtitle from exact shot timings: {len(timings)} cues"
                )
                return subtitle_path
            except Exception as exc:
                logger.warning(
                    f"failed to write shot-timing subtitles: {type(exc).__name__}"
                )

    if sub_maker is None and subtitle_provider != "whisper":
        # 自定义音频不会经过 TTS，因此没有 Edge/Azure 等 TTS 返回的
        # sub_maker 时间轴。只有 Whisper 可以直接从音频文件转写字幕；
        # 其他字幕提供方继续保持原有行为，避免生成错误的空时间轴。
        logger.warning(
            "subtitle maker is missing, skip subtitle generation for provider: "
            f"{subtitle_provider}"
        )
        return ""

    is_word_level = getattr(params, "subtitle_display_mode", "sentence") == "word_by_word"

    # A failed retry must never reuse captions from an earlier narration.
    with staged_subtitle_file(final_subtitle_path) as subtitle_path:
        if subtitle_provider == "edge":
            voice.create_subtitle(
                text=video_script,
                sub_maker=sub_maker,
                subtitle_file=subtitle_path,
                word_level=is_word_level,
            )
            if not os.path.exists(subtitle_path):
                # Edge 字幕偶尔会因为时间轴与文案无法匹配而没有产出文件。这里不能
                # 自动切换到 Whisper，否则首次失败会在用户不知情的情况下下载数 GB
                # 的模型。只有显式配置 Whisper 时才允许加载模型，Edge 失败则保留
                # 无字幕视频并记录原因，避免意外的网络和磁盘开销。
                logger.warning(
                    "edge subtitle generation did not produce a subtitle file; "
                    "skip subtitles without falling back to whisper"
                )
                return ""

        if subtitle_provider == "whisper":
            subtitle.create(
                audio_file=audio_file,
                subtitle_file=subtitle_path,
                word_level=is_word_level,
            )
            if not subtitle.file_to_subtitles(subtitle_path):
                logger.warning("whisper produced no usable subtitle cues")
                return ""
            if not is_word_level:
                logger.info("\n\n## correcting subtitle")
                subtitle.correct(subtitle_file=subtitle_path, video_script=video_script)

        subtitle_lines = subtitle.file_to_subtitles(subtitle_path)
        if not subtitle_lines:
            logger.warning(f"subtitle file is invalid: {subtitle_path}")
            return ""

        os.replace(subtitle_path, final_subtitle_path)
        return final_subtitle_path


def _shot_visual_types(params, video_terms: list) -> list | None:
    """Per-shot visual_type list aligned with ``video_terms``.

    The structured biography script carries ``visual_type`` on every timeline
    entry; ``script_to_search_terms`` builds terms and types in the same order,
    so the lists line up 1:1. Without a structured script a biography task
    treats every shot as a historical_photo. Returns None for non-biography
    sources, which keep their existing flow.
    """
    if getattr(params, "video_source", "") != "historical_archive":
        return None
    script = getattr(params, "biography_script", None)
    if isinstance(script, dict):
        from app.services import biography

        types = biography.script_shot_visual_types(script)
        terms = biography.script_to_search_terms(script)
        # Only trust the pairing when the terms actually became video_terms.
        if types and len(types) == len(terms):
            if not video_terms or list(video_terms) == terms:
                return types
        timeline = script.get("timeline") or []
        fallback = [
            str(shot.get("visual_type") or "historical_photo").strip()
            for shot in timeline
            if isinstance(shot, dict)
        ]
        if fallback:
            return fallback
    return ["historical_photo"] * len(video_terms or [])


def _download_from_source(
    *,
    task_id,
    params,
    video_terms,
    audio_duration,
    source: str,
    shot_visual_types: list | None,
):
    """Download materials from one source, isolating failure handling.

    Extracted so ``historical_archive`` can try the archive first and then
    fall back to stock b-roll without duplicating the error semantics of the
    existing dispatch (each provider raises its own error type).
    """
    try:
        return material.download_videos(
            task_id=task_id,
            search_terms=video_terms,
            source=source,
            video_aspect=params.video_aspect,
            video_concat_mode=(
                VideoConcatMode.sequential
                if params.match_materials_to_script
                else params.video_concat_mode
            ),
            audio_duration=audio_duration * params.video_count,
            max_clip_duration=params.video_clip_duration,
            match_script_order=params.match_materials_to_script,
            shot_visual_types=shot_visual_types,
            person_name=getattr(params, "biography_person", "") or "",
        )
    except Exception as exc:
        logger.error(
            f"failed to download materials from {source}: "
            f"error={type(exc).__name__}: {exc}"
        )
        return []


def get_video_materials(
    task_id,
    params,
    video_terms,
    audio_duration,
    loomloom_video_request: loomloom.LoomLoomConfirmedVideoRequest | None = None,
    shot_visual_types: list | None = None,
):
    if params.video_source == "local":
        logger.info("\n\n## preprocess local materials")
        materials = video.preprocess_video(
            materials=params.video_materials, clip_duration=params.video_clip_duration
        )
        if not materials:
            _mark_task_failed(
                task_id,
                "materials",
                "no valid local video materials were found",
            )
            return None
        return [material_info.url for material_info in materials]
    elif params.video_source == "loomloom":
        if not isinstance(
            loomloom_video_request, loomloom.LoomLoomConfirmedVideoRequest
        ):
            _mark_task_failed(
                task_id,
                "materials",
                "LoomLoom video generation requires a confirmed quote",
            )
            return None

        request = loomloom_video_request
        logger.info(
            "\n\n## generating "
            f"{len(request.batch.input_rows)} video materials with LoomLoom"
        )
        run_id = ""
        try:
            request.validate()
            backend = loomloom.LoomLoomVideoBackend(request.settings)
            execution = backend.execute(
                request.batch,
                client_request_id=request.client_request_id,
                listing_version_id=request.listing_version_id,
                confirm=True,
            )
            run_id = execution.run_id
            # execute 返回即表示付费任务已经由远端接受。必须先把 run ID 写入
            # 进程日志，即使 Redis 等状态后端随后不可用，运维人员仍能凭日志
            # 在胜算云侧定位任务，不能让唯一标识只存在于局部变量中。
            logger.info(
                "LoomLoom paid video run created: "
                f"task_id={task_id}, run_id={run_id}, "
                f"listing_version_id={request.listing_version_id}"
            )
            # 付费任务一旦创建就立即记录远端 ID。即使后续轮询超时，日志和任务
            # 状态仍能帮助用户或平台支持人员定位并找回已经生成的产物。状态后端
            # 故障只能降低可观测性，不能中断已经开始计费的远端任务和产物下载。
            _record_loomloom_run_reference(
                task_id=task_id,
                run_id=run_id,
                listing_version_id=request.listing_version_id,
            )
            backend.wait_for_run(run_id)
            return list(
                backend.download_video_results(
                    run_id,
                    utils.task_dir(task_id),
                )
            )
        except (loomloom.LoomLoomError, ValueError) as exc:
            _mark_task_failed(
                task_id,
                "materials",
                str(exc),
                details={
                    "loomloom_run_id": run_id,
                    "loomloom_listing_version_id": request.listing_version_id,
                },
            )
            return None
    else:
        logger.info(f"\n\n## downloading videos from {params.video_source}")
        # 传记素材源:优先用人物真实历史照片;若该人物在公开档案中确实没有
        # 可用素材(冷僻人物/纯外国小人物),整段降级到库存源做过渡画面,
        # 而不是让任务失败。
        if params.video_source == "historical_archive":
            archive_clips = _download_from_source(
                task_id=task_id,
                params=params,
                video_terms=video_terms,
                audio_duration=audio_duration,
                source="historical_archive",
                shot_visual_types=shot_visual_types,
            )
            if archive_clips:
                return archive_clips
            logger.warning(
                "historical_archive returned no usable material, "
                "falling back to stock b-roll"
            )
            # b_roll 兜底:仅过渡镜头,不用于展示人物本人的镜头。
            fallback = _download_from_source(
                task_id=task_id,
                params=params,
                video_terms=video_terms,
                audio_duration=audio_duration,
                source=params.material_fallback_source or "pexels",
                shot_visual_types=None,
            )
            return fallback
        try:
            downloaded_videos = material.download_videos(
                task_id=task_id,
                search_terms=video_terms,
                source=params.video_source,
                video_aspect=params.video_aspect,
                video_concat_mode=(
                    VideoConcatMode.sequential
                    if params.match_materials_to_script
                    else params.video_concat_mode
                ),
                audio_duration=audio_duration * params.video_count,
                max_clip_duration=params.video_clip_duration,
                match_script_order=params.match_materials_to_script,
                shot_visual_types=shot_visual_types,
                person_name=getattr(params, "biography_person", "") or "",
                # 素材阶段占用 40%~50%：每下完一个文件推进一次，慢速网络下
                # 进度条不再长时间停在 40%。
                progress_callback=_stage_progress_reporter(task_id, 40, 50),
            )
        except volcengine_seedance.VolcEngineSeedanceError as exc:
            # 未确认状态和已生成但下载失败都对应一个可在方舟控制台恢复的远端
            # 任务。统一从异常携带的 task_id 写入失败状态，避免不同异常分支
            # 各自维护恢复信息并在后续扩展时再次遗漏。
            remote_task_id = str(getattr(exc, "task_id", "") or "").strip()
            details = (
                {"volcengine_seedance_task_id": remote_task_id}
                if remote_task_id
                else None
            )
            _mark_task_failed(
                task_id,
                "materials",
                str(exc),
                details=details,
            )
            return None
        except (
            material.WaveSpeedUnconfirmedTaskError,
            material.WaveSpeedDownloadError,
        ) as exc:
            # Once a paid prediction exists, a polling or download failure must
            # leave its ID in task state. Earlier successful clips cannot turn
            # this incomplete run into an apparently completed video task.
            prediction_id = str(getattr(exc, "prediction_id", "") or "").strip()
            details = {"wavespeed_prediction_id": prediction_id} if prediction_id else None
            _mark_task_failed(task_id, "materials", str(exc), details=details)
            return None
        except material.OpenAIImagePaidResultError as exc:
            # A paid request with an uncertain response, failed result download,
            # or failed local render must stop before another image order.
            _mark_task_failed(task_id, "materials", str(exc))
            return None
        except ofox.OFoxError as exc:
            # 与方舟同一恢复语义：未确认状态和已生成但下载失败都对应一个可在
            # OFox 控制台恢复的远端任务，统一从异常携带的 task_id 写入失败状态。
            remote_task_id = str(getattr(exc, "task_id", "") or "").strip()
            details = (
                {"ofox_task_id": remote_task_id} if remote_task_id else None
            )
            _mark_task_failed(
                task_id,
                "materials",
                str(exc),
                details=details,
            )
            return None
        except metaso_minimax.MetasoMiniMaxError as exc:
            # 秘塔任务与方舟任务使用不同的恢复入口和字段名，不能合并成一个
            # 模糊的 remote_task_id。保留明确 Provider 前缀便于 API、WebUI
            # 和运维日志直接定位对应平台。
            remote_task_id = str(getattr(exc, "task_id", "") or "").strip()
            details = (
                {"metaso_minimax_task_id": remote_task_id} if remote_task_id else None
            )
            _mark_task_failed(
                task_id,
                "materials",
                str(exc),
                details=details,
            )
            return None
        except muapi.MuAPIError as exc:
            # MuAPI tasks are billable after acceptance.  Preserve the remote
            # request ID on any ambiguous or post-completion failure so the
            # user can recover it from the provider dashboard.
            remote_task_id = str(getattr(exc, "task_id", "") or "").strip()
            details = {"muapi_task_id": remote_task_id} if remote_task_id else None
            _mark_task_failed(
                task_id,
                "materials",
                str(exc),
                details=details,
            )
            return None
        if not downloaded_videos:
            _mark_task_failed(
                task_id,
                "materials",
                f"failed to download video materials from {params.video_source}",
            )
            return None
        return downloaded_videos


def _record_loomloom_run_reference(
    *, task_id: str, run_id: str, listing_version_id: str
) -> bool | None:
    """
    尽最大努力保存已创建的付费 LoomLoom Run，不让状态故障中断远端任务。

    返回 True 表示保存成功，False 表示任务记录已经不存在，None 表示状态后端
    在有限重试后仍不可用。调用方无论得到哪种结果都应继续轮询和下载，因为
    execute 已经产生外部付费副作用，停止本地流程只会让产物更难找回。
    """
    fields = {
        "loomloom_run_id": run_id,
        "loomloom_listing_version_id": listing_version_id,
    }
    for attempt in range(1, _LOOMLOOM_STATE_WRITE_ATTEMPTS + 1):
        try:
            updated = sm.state.patch_task(task_id, **fields)
        except Exception as exc:
            if attempt >= _LOOMLOOM_STATE_WRITE_ATTEMPTS:
                logger.exception(
                    "failed to persist LoomLoom paid run after retries: "
                    f"task_id={task_id}, run_id={run_id}, attempts={attempt}, "
                    f"error={exc}"
                )
                return None
            logger.warning(
                "retry LoomLoom paid run state update: "
                f"task_id={task_id}, run_id={run_id}, attempt={attempt}, "
                f"error={exc}"
            )
            time.sleep(_LOOMLOOM_STATE_RETRY_DELAY_SECONDS)
            continue

        if updated is False:
            logger.warning(
                "could not persist LoomLoom paid run because task is missing: "
                f"task_id={task_id}, run_id={run_id}"
            )
        return updated

    return None


def _get_material_source_groups(task_id: str, video_paths: list[str]) -> dict[str, str]:
    """Recover keyword groups from the downloaded material manifest."""
    try:
        with open(path.join(utils.task_dir(task_id), "script.json"), encoding="utf-8") as file:
            payload = json.load(file)
        groups = {
            record["local_file"]: record["search_term"]
            for record in payload.get("material_sources", [])
            if isinstance(record, dict)
            and isinstance(record.get("local_file"), str)
            and isinstance(record.get("search_term"), str)
            and record["search_term"]
        }
        return {
            file: groups[path.basename(file)]
            for file in video_paths
            if path.basename(file) in groups
        }
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        logger.warning(f"Cannot read material keyword groups: task_id={task_id}, error={exc}")
        return {}


def _load_material_sources(task_id: str) -> list:
    """Read back the material source records written during download.

    The sidecar needs licenses for every image actually used, and those are
    persisted by the material service (not held in memory across the pipeline).
    Returns [] when the task file is missing or malformed.
    """
    try:
        script_file = path.join(utils.task_dir(task_id), "script.json")
        if not path.isfile(script_file):
            return []
        with open(script_file, "r", encoding="utf-8") as f:
            payload = json.load(f)
        sources = payload.get("material_sources")
        return sources if isinstance(sources, list) else []
    except Exception as exc:
        logger.warning(f"failed to load material sources: {type(exc).__name__}")
        return []


def generate_final_videos(
    task_id, params, downloaded_videos, audio_file, subtitle_path, audio_duration,
    *, partial_results: dict | None = None,
):
    """Record completed outputs even if a later clip raises before returning."""
    final_video_paths = []
    combined_video_paths = []
    warnings = []
    if partial_results is not None:
        # The caller owns this snapshot so it can report completed files even
        # when the state backend fails during a progress/checkpoint write.
        partial_results.update(
            videos=final_video_paths, combined_videos=combined_video_paths,
            warnings=warnings,
        )
    allocate_batch_materials = params.video_count > 1 and params.video_source in {
        "pexels", "pixabay", "coverr", "local"
    }
    source_usage = {}
    material_selections = []
    source_groups = (
        _get_material_source_groups(task_id, downloaded_videos)
        if (allocate_batch_materials and params.match_materials_to_script
            and params.video_source != "local")
        else {}
    )
    video_music_provider = _VIDEO_MUSIC_PROVIDERS.get(params.bgm_type)
    video_music_requested = (
        video_music_provider is not None
        and bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume)
    )
    # Matching preserves keyword order; batch allocation varies each keyword's candidates.
    if params.match_materials_to_script:
        video_concat_mode = VideoConcatMode.sequential
    elif params.video_count == 1:
        video_concat_mode = params.video_concat_mode
    else:
        video_concat_mode = VideoConcatMode.random
    video_transition_mode = params.video_transition_mode

    _progress = 50
    for i in range(params.video_count):
        index = i + 1
        if partial_results is not None:
            partial_results["failed_video_index"] = index
        combined_video_path = path.join(
            utils.task_dir(task_id), f"combined-{index}.mp4"
        )
        logger.info(f"\n\n## combining video: {index} => {combined_video_path}")
        used_video_paths = []
        batch_options = (
            {
                "source_usage": source_usage,
                "source_groups": source_groups,
                "used_video_paths": used_video_paths,
            }
            if allocate_batch_materials else {}
        )
        # 每条视频占 50% 进度中的一份，合成与最终编码各占一半。片段处理是
        # 合成里最耗时的部分，逐段推进这一半的进度。
        combine_share = 50 / params.video_count / 2
        video.combine_videos(
            combined_video_path=combined_video_path,
            video_paths=downloaded_videos,
            audio_file=audio_file,
            video_aspect=params.video_aspect,
            video_fit_mode=params.video_fit_mode,
            video_concat_mode=video_concat_mode,
            video_transition_mode=video_transition_mode,
            max_clip_duration=params.video_clip_duration,
            threads=params.n_threads,
            clip_speed=params.video_clip_speed,
            progress_callback=_stage_progress_reporter(
                task_id, _progress, _progress + combine_share
            ),
            **batch_options,
        )
        if allocate_batch_materials:
            selected_sources = list(dict.fromkeys(used_video_paths))
            reused_sources = [file for file in selected_sources if source_usage.get(file, 0)]
            for file in selected_sources:
                source_usage[file] = source_usage.get(file, 0) + 1
            material_selections.append({
                "video_index": index,
                "local_files": [path.basename(file) for file in used_video_paths],
                "reused_files": [path.basename(file) for file in reused_sources],
            })
            task_artifacts.patch_script_data(task_id, material_selections=material_selections)
            logger.info(
                f"Batch material allocation: video_index={index}, "
                f"sources={len(selected_sources)}, reused_sources={len(reused_sources)}"
            )
            if reused_sources:
                warnings.append({
                    "code": "batch_materials_reused",
                    "video_index": index,
                    "count": len(reused_sources),
                })

        _progress += 50 / params.video_count / 2
        sm.state.update_task(task_id, progress=_progress)

        final_video_path = path.join(utils.task_dir(task_id), f"final-{index}.mp4")

        # 视频配乐模式先明确禁用默认 BGM 解析，避免旧任务残留的 bgm_file 被
        # 误用。只有音量大于 0 才生成代理并调用付费 API；0 音量统一跳过。
        bgm_file_override = "" if video_music_provider else None
        if video_music_requested:
            service = video_music_provider["service"]
            display_name = video_music_provider["display_name"]
            warning_code = video_music_provider["warning_code"]
            generated_bgm_path = path.join(
                utils.task_dir(task_id),
                (f"{params.bgm_type}-bgm-{index}{video_music_provider['suffix']}"),
            )
            try:
                service.generate_bgm(
                    video_path=combined_video_path,
                    output_path=generated_bgm_path,
                    video_duration=audio_duration,
                    prompt=_get_video_music_prompt(params),
                )
                bgm_file_override = generated_bgm_path
            except video_music_provider["error_type"] as exc:
                # 视频、旁白和字幕都已生成时，第三方配乐临时失败不应浪费整条
                # 任务。当前视频明确禁用 BGM，并把降级结果返回 WebUI 提醒用户。
                logger.warning(
                    f"{display_name} BGM generation failed: task_id={task_id}, "
                    f"video_index={index}, error={exc}"
                )
                bgm_file_override = ""
                warnings.append({"code": warning_code, "video_index": index})

        logger.info(f"\n\n## generating video: {index} => {final_video_path}")
        bgm_mix_succeeded = video.generate_video(
            video_path=combined_video_path,
            audio_path=audio_file,
            subtitle_path=subtitle_path,
            output_file=final_video_path,
            params=params,
            bgm_file_override=bgm_file_override,
        )
        if (
            video_music_provider is not None
            and bgm_file_override
            and not bgm_mix_succeeded
        ):
            # 第三方已成功返回并通过 FFmpeg 校验，但 MoviePy 最终混音仍可能
            # 因运行环境失败。视频服务会保留无 BGM 成片；API 生成失败时
            # override 为空，因此不会重复追加警告。
            warnings.append(
                {
                    "code": video_music_provider["warning_code"],
                    "video_index": index,
                }
            )

        final_video_paths.append(final_video_path)
        combined_video_paths.append(combined_video_path)
        _progress += 50 / params.video_count / 2
        sm.state.update_task(
            task_id, progress=_progress, videos=list(final_video_paths),
            combined_videos=list(combined_video_paths), warnings=list(warnings) or None,
        )

    return final_video_paths, combined_video_paths, warnings


def _patch_cross_post_state(task_id: str, **kwargs) -> bool | None:
    """安全更新发布字段；短暂状态后端故障时有限重试。"""
    for attempt in range(1, _CROSS_POST_STATE_WRITE_ATTEMPTS + 1):
        try:
            return sm.state.patch_task(task_id, **kwargs)
        except Exception as exc:
            # Redis 短暂断连不应让任务永久停留在 pending/processing。发布状态
            # 写入频率很低，这里使用固定次数和短等待即可覆盖瞬时故障，同时
            # 避免后台线程无限阻塞。最后一次失败保留完整堆栈便于定位。
            if attempt >= _CROSS_POST_STATE_WRITE_ATTEMPTS:
                logger.exception(
                    f"failed to update cross-post state after retries, "
                    f"task_id: {task_id}, fields: {', '.join(kwargs)}, "
                    f"attempts: {attempt}, error: {exc}"
                )
                return None

            logger.warning(
                f"retry cross-post state update, task_id: {task_id}, "
                f"fields: {', '.join(kwargs)}, attempt: {attempt}, error: {exc}"
            )
            time.sleep(_CROSS_POST_STATE_RETRY_DELAY_SECONDS)

    return None


def _record_cross_post_failure(
    task_id: str,
    error: Exception,
    results: list[dict] | None = None,
) -> None:
    """尽最大努力保存发布失败；状态后端不可用时由日志保留诊断信息。"""
    updated = _patch_cross_post_state(
        task_id,
        cross_post_state=const.CROSS_POST_STATE_FAILED,
        cross_post_results=results or None,
        cross_post_error=str(error),
        cross_post_owner=None,
    )
    if updated is False:
        logger.warning(f"discard cross-post failure for missing task: {task_id}")


def _ensure_cross_post_terminal_state(task_id: str) -> None:
    """Future 结束后把仍处于活动态的任务收敛为失败。"""
    try:
        task = sm.state.get_task(task_id)
    except Exception as exc:
        # 此处已经是 Future 的最终回调，没有后续同步调用方可以处理异常。
        # 状态后端恢复后，下一次进程启动仍会通过恢复逻辑处理遗留状态。
        logger.exception(
            f"failed to verify final cross-post state, task_id: {task_id}, error: {exc}"
        )
        return

    if not task or task.get("cross_post_state") not in _ACTIVE_CROSS_POST_STATES:
        return

    logger.warning(
        f"cross-post worker ended without terminal state, task_id: {task_id}, "
        f"state: {task.get('cross_post_state')}"
    )
    _record_cross_post_failure(
        task_id,
        RuntimeError("cross-post worker ended without persisting a terminal state"),
        task.get("cross_post_results"),
    )


def recover_interrupted_cross_posts(page_size: int = 100) -> int | None:
    """
    将进程重启后无法恢复的发布任务标记为失败。

    跨平台发布使用当前进程内的线程池，不是持久化任务队列。进程启动时，
    Redis 中残留的 pending/processing 不会自动继续执行；如果继续把它们视为
    运行中，用户将永久无法删除任务。这里仅收集一次任务 ID，再读取当前状态；
    不能跨多次 Redis SCAN 按页码推进，否则扫描顺序变化会漏掉遗留任务。
    只处理当前进程没有对应 Future 的活动记录，并保留已经生成的视频结果。
    """
    recovered = 0
    try:
        task_ids = sm.state.list_task_ids(scan_count=page_size)
        for task_id in task_ids:
            # 扫描之后任务可能已被删除或转为终态；以最新状态决定是否恢复，
            # 不能使用扫描时的旧快照覆盖已经完成的发布结果。
            task = sm.state.get_task(task_id)
            if (
                not task
                or task.get("cross_post_state") not in _ACTIVE_CROSS_POST_STATES
                or _is_cross_post_active_in_process(task_id)
                or _is_cross_post_owner_alive(task.get("cross_post_owner"))
            ):
                continue

            updated = _patch_cross_post_state(
                task_id,
                cross_post_state=const.CROSS_POST_STATE_FAILED,
                cross_post_error=_INTERRUPTED_CROSS_POST_ERROR,
                cross_post_owner=None,
            )
            if updated is True:
                recovered += 1
    except Exception as exc:
        logger.exception(f"failed to recover interrupted cross-post tasks: {exc}")
        return None

    if recovered:
        logger.warning(f"recovered interrupted cross-post tasks: {recovered}")
    return recovered


def _run_cross_post(
    task_id: str,
    video_paths: tuple[str, ...],
    video_subject: str,
    video_script: str,
    video_language: str,
    platforms: tuple[str, ...],
    youtube_privacy_status: str,
    youtube_made_for_kids: bool = False,
    upload_account: dict | None = None,
) -> None:
    """后台执行跨平台发布，并只补充发布相关的任务字段。"""
    results = []
    try:
        state_updated = _patch_cross_post_state(
            task_id,
            cross_post_state=const.CROSS_POST_STATE_PROCESSING,
            cross_post_error=None,
            cross_post_owner=_cross_post_process_owner,
        )
        if state_updated is not True:
            # False 表示任务已删除，None 表示状态后端暂时不可用。两种情况都
            # 不应继续调用第三方接口，否则用户无法查询或控制这次发布。
            if state_updated is False:
                logger.warning(f"skip cross-post for missing task: {task_id}")
            else:
                _record_cross_post_failure(
                    task_id,
                    RuntimeError("failed to persist cross-post processing state"),
                )
            return

        logger.info(
            f"cross-post started, task_id: {task_id}, platforms: {', '.join(platforms)}"
        )
        youtube_extra = None
        post_title = video_subject or "Check out this video! #shorts #viral"
        if platforms:
            has_youtube = any(platform.startswith("youtube") for platform in platforms)
            social_platform = "youtube_shorts"
            if not has_youtube:
                first = (platforms[0] or "").strip().lower()
                # llm.py resolves unknown ids to its default platform.
                social_platform = _CROSS_POST_SOCIAL_PLATFORMS.get(first, first)
            metadata = llm.generate_social_metadata(
                video_subject=video_subject,
                video_script=video_script,
                language=video_language or "",
                platform=social_platform,
            )
            if has_youtube:
                youtube_extra = {
                    "youtube_title": metadata.get("title", video_subject),
                    "youtube_description": metadata.get("caption", ""),
                    "tags": metadata.get("hashtags", []),
                    "privacyStatus": youtube_privacy_status,
                    "selfDeclaredMadeForKids": youtube_made_for_kids,
                    "containsSyntheticMedia": True,
                }
            post_title = (
                metadata.get("caption")
                or metadata.get("title")
                or video_subject
                or "Check out this video! #shorts #viral"
            )

        for video_path in video_paths:
            pending_result_index = None

            def record_background_request(request_id: str) -> None:
                nonlocal pending_result_index
                # Persist the remote handle before polling. If this process
                # exits mid-upload, recovery can expose the ID to the user.
                pending_result_index = len(results)
                results.append(
                    {
                        "video_index": pending_result_index + 1,
                        "request_id": request_id,
                        "status": "processing",
                    }
                )
                if _patch_cross_post_state(
                    task_id, cross_post_results=list(results)
                ) is not True:
                    logger.warning(
                        "could not persist background upload request ID: "
                        f"task_id={task_id}, request_id={request_id}"
                    )

            upload_kwargs = {}
            if upload_account is not None:
                upload_kwargs["account"] = upload_account
            result = upload_post.cross_post_video(
                video_path=video_path,
                title=post_title,
                platforms=list(platforms),
                youtube_extra=youtube_extra,
                on_background_start=record_background_request,
                **upload_kwargs,
            )
            if not isinstance(result, dict):
                result = {
                    "success": False,
                    "error": "Upload-Post returned an invalid response",
                }
            if pending_result_index is None:
                results.append(result)
            else:
                results[pending_result_index] = result

        failures = [result for result in results if not result.get("success")]
        if failures:
            error_messages = [
                str(
                    result.get("error")
                    or result.get("message")
                    or "unknown upload error"
                )
                for result in failures
            ]
            cross_post_state = const.CROSS_POST_STATE_FAILED
            cross_post_error = "; ".join(error_messages)
            logger.warning(
                f"cross-post completed with failures, task_id: {task_id}, "
                f"failed: {len(failures)}, total: {len(results)}"
            )
        else:
            cross_post_state = const.CROSS_POST_STATE_COMPLETE
            cross_post_error = None
            logger.success(
                f"cross-post completed, task_id: {task_id}, videos: {len(results)}"
            )

        state_updated = _patch_cross_post_state(
            task_id,
            cross_post_state=cross_post_state,
            cross_post_results=results,
            cross_post_error=cross_post_error,
            cross_post_owner=None,
        )
        if state_updated is False:
            logger.warning(f"discard cross-post result for missing task: {task_id}")
        elif state_updated is None:
            # 上传已经结束但结果没有持久化时，不能继续保留 processing。
            # 失败状态写入会再次经过有限重试，至少让调用方得到明确终态。
            _record_cross_post_failure(
                task_id,
                RuntimeError("failed to persist final cross-post result"),
                results,
            )
    except Exception as exc:
        # 发布失败只影响发布状态，不能反向覆盖已经完成的视频任务。
        # 异常原文写入任务状态，API 调用方无需访问服务端日志也能定位问题。
        logger.exception(f"cross-post failed, task_id: {task_id}, error: {exc}")
        _record_cross_post_failure(task_id, exc, results)


def _run_cross_post_with_slot(*args) -> None:
    """执行发布任务，并确保成功、失败或异常时都会归还队列容量。"""
    try:
        _run_cross_post(*args)
    except Exception as exc:
        # _run_cross_post 已处理预期异常；这里是最后一道保护，避免未来新增
        # 逻辑抛出的异常只保存在无人读取的 Future 中。
        task_id = str(args[0]) if args else "unknown"
        logger.exception(f"cross-post worker crashed, task_id: {task_id}, error: {exc}")
        if args:
            _record_cross_post_failure(task_id, exc)
    finally:
        _cross_post_slots.release()


def _finalize_cross_post_future(task_id: str, future: Future) -> None:
    """清理 Future 注册，并确保取消、异常和状态写入失败都能收敛。"""
    _unregister_cross_post_future(task_id, future)

    try:
        error = future.exception()
    except CancelledError:
        logger.warning(f"cross-post future was cancelled, task_id: {task_id}")
        # Future 在开始执行前被取消时，worker 的 finally 不会运行，因此需要
        # 在回调中归还队列容量，并把持久化状态改为失败。
        _cross_post_slots.release()
        _record_cross_post_failure(
            task_id,
            RuntimeError("cross-post job was cancelled before execution"),
        )
        return
    except Exception as exc:
        logger.exception(
            f"failed to inspect cross-post future, task_id: {task_id}, error: {exc}"
        )
        _ensure_cross_post_terminal_state(task_id)
        return

    if error is not None:
        logger.error(
            f"cross-post future failed, task_id: {task_id}, "
            f"error: {type(error).__name__}: {error}"
        )

    _ensure_cross_post_terminal_state(task_id)


def _schedule_cross_post(
    task_id: str,
    video_paths: list[str],
    params: VideoParams,
    video_script: str,
    platforms: list[str],
    youtube_privacy_status: str,
    youtube_made_for_kids: bool = False,
) -> str | None:
    """提交后台发布任务；成功返回 None，调度失败返回可查询的错误原因。"""
    if not _cross_post_slots.acquire(blocking=False):
        error = "cross-post queue is full; publishing was skipped"
        logger.warning(
            f"skip cross-post because queue is full, task_id: {task_id}, "
            f"capacity: {_cross_post_max_pending_tasks}"
        )
        _patch_cross_post_state(
            task_id,
            cross_post_state=const.CROSS_POST_STATE_FAILED,
            cross_post_error=error,
            cross_post_owner=None,
        )
        return error

    try:
        future = _cross_post_executor.submit(
            _run_cross_post_with_slot,
            task_id,
            tuple(video_paths),
            params.video_subject or "",
            video_script,
            params.video_language or "",
            tuple(platforms),
            youtube_privacy_status,
            youtube_made_for_kids,
            upload_post.upload_post_service.snapshot_account(),
        )
        _register_cross_post_future(task_id, future)
        future.add_done_callback(partial(_finalize_cross_post_future, task_id))
    except RuntimeError as exc:
        _unregister_cross_post_future(task_id)
        _cross_post_slots.release()
        logger.exception(
            f"failed to schedule cross-post, task_id: {task_id}, error: {exc}"
        )
        _patch_cross_post_state(
            task_id,
            cross_post_state=const.CROSS_POST_STATE_FAILED,
            cross_post_error=f"failed to schedule cross-post: {exc}",
            cross_post_owner=None,
        )
        return f"failed to schedule cross-post: {exc}"

    return None


_STORYBOARD_STAGE_FILENAME = "storyboard.json"


def _storyboard_stage_path(task_id) -> str:
    return os.path.join(utils.task_dir(task_id), _STORYBOARD_STAGE_FILENAME)


def _stage_storyboard(task_id, storyboard: dict) -> None:
    """把分镜暂存到任务目录的 storyboard.json。

    用独立文件而非进程级字典:任务间互不干扰、长跑进程不泄漏,且不依赖
    script.json(generate_script 阶段它还不存在)。写入失败只记录警告。
    """
    try:
        os.makedirs(utils.task_dir(task_id), exist_ok=True)
        with open(_storyboard_stage_path(task_id), "w", encoding="utf-8") as f:
            json.dump(storyboard, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.warning(f"failed to stage storyboard: {type(exc).__name__}: {exc}")


def _load_staged_storyboard(task_id) -> dict | None:
    """读取暂存的分镜(无则 None)。读取失败一律返回 None,由调用方回退。"""
    try:
        path = _storyboard_stage_path(task_id)
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            sb = json.load(f)
        return sb if isinstance(sb, dict) and sb.get("timeline") else None
    except Exception:
        return None


def _normalize_voice_params(params) -> None:
    """把缺失/损坏的配音配置补齐为配置中的可用默认值。

    真实故障(已复现):WebUI 的 [ui] 段曾写入 ``voice_name=""``,而 [app] 段有正确
    的 ``voice_name="zh-CN-XiaoxiaoNeural"``。流水线只读传入的 params,于是空音色
    一路走到音频阶段才失败。这里在任务开始时把空音色回填为配置默认值,让"开箱
    即用"成立,而不是让每个调用方都记得显式传音色。

    只在 voice_name 为空时回填;明确的"无配音"哨兵与用户已选音色都不动。
    """
    voice_name = str(getattr(params, "voice_name", "") or "").strip()
    if voice_name:
        return
    if getattr(params, "custom_audio_file", None):
        return
    for candidate in (
        config.ui.get("voice_name", ""),
        config.app.get("voice_name", ""),
    ):
        candidate = str(candidate or "").strip()
        if candidate and not voice.is_no_voice(candidate):
            params.voice_name = candidate
            logger.info(f"voice_name empty; resolved default voice: {candidate}")
            return


def _voice_preflight_error(params) -> str | None:
    """在生成脚本前校验配音配置,返回可操作错误或 None。

    生产环境最常见的"根本无法使用"来自配置自相矛盾:WebUI 的 [ui] 段落里
    ``voice_mode="none"`` / ``tts_server="elevenlabs"`` / ``voice_name=""`` 三处
    互相冲突,流水线读到空音色后要到音频阶段才失败。这里把冲突提前暴露。

    判定顺序:
    1. 有自定义音频文件 → 跳过(用户上传的音频优先于 TTS)。
    2. voice_name 为空且不是明确的"无配音"哨兵 → 报错(空串更可能是配置损坏,
       不能静默当无配音;这与 voice.is_no_voice 的设计一致)。
    3. 明确的"无配音"哨兵 → 通过(静音分支合法)。
    4. 付费 TTS provider 缺少凭据 → 报错。
    """
    requested_custom_audio_file = getattr(params, "custom_audio_file", None)
    if requested_custom_audio_file:
        return None

    voice_name = str(getattr(params, "voice_name", "") or "")
    stripped = voice_name.strip()

    if not stripped:
        return (
            "voice_name is empty; set a voice in Settings or send a valid "
            "voice_name (e.g. zh-CN-XiaoxiaoNeural). The WebUI [ui] section "
            "may hold a stale empty voice_name while [app] has the real one."
        )

    if voice.is_no_voice(stripped):
        # 明确的无配音:合法静音分支,不校验 provider 凭据。
        return None

    # 付费/需凭据的 TTS provider:缺 Key 必须提前失败,而不是等到调用时返回 None。
    missing_credentials = {
        "elevenlabs": (voice.is_elevenlabs_voice, voice.get_elevenlabs_api_key, "ElevenLabs"),
        "fish_audio": (voice.is_fish_audio_voice, voice.get_fish_audio_api_key, "Fish Audio"),
        "minimax": (voice.is_minimax_voice, voice.get_minimax_tts_api_key, "MiniMax TTS"),
    }
    for provider_id, (is_voice, get_key, display) in missing_credentials.items():
        try:
            if is_voice(stripped) and not str(get_key() or "").strip():
                return (
                    f"{display} voice selected but its API key is empty; "
                    f"set the {provider_id} key in Settings or choose another voice."
                )
        except Exception as exc:  # pragma: no cover - 凭据探测失败不应阻断任务
            logger.warning(f"voice preflight credential probe failed for {provider_id}: {exc}")

    return None


def _run_pipeline(
    task_id,
    params: VideoParams,
    stop_at: str = "video",
    voice_preview: dict | None = None,
    loomloom_video_request: loomloom.LoomLoomConfirmedVideoRequest | None = None,
    allow_server_file_input: bool = False,
    voxcpm_reference_audio: bytes | None = None,
    voxcpm_prompt_audio: bytes | None = None,
    voxcpm_prompt_text: str = "",
):
    logger.info(f"start task: {task_id}, stop_at: {stop_at}")
    sm.state.update_task(
        task_id, state=const.TASK_STATE_PROCESSING, progress=5,
        failed_stage=None, error=None,
        videos=[], combined_videos=[], warnings=None, failed_video_index=None,
    )

    if stop_at == "video" and params.subtitle_enabled:
        try:
            video.validate_subtitle_colors(params)
        except ValueError as exc:
            return _mark_task_failed(task_id, "preflight", str(exc))

    if stop_at in {"materials", "video"} and params.video_source not in _SUPPORTED_VIDEO_SOURCES:
        return _mark_task_failed(
            task_id, "preflight", f"unsupported video source: {params.video_source!r}"
        )

    if (
        stop_at in {"materials", "video"}
        and params.video_source == "volcengine_seedance"
        and not volcengine_seedance.is_enabled()
    ):
        return _mark_task_failed(
            task_id,
            "preflight",
            "Volcano Engine Seedance requires an Ark API key",
        )

    if (
        stop_at in {"materials", "video"}
        and params.video_source == "ofox"
        and not ofox.is_enabled()
    ):
        return _mark_task_failed(
            task_id,
            "preflight",
            "OFox video generation requires an OFox API key",
        )

    if (
        stop_at in {"materials", "video"}
        and params.video_source == "metaso_minimax"
        and not metaso_minimax.is_enabled()
    ):
        return _mark_task_failed(
            task_id,
            "preflight",
            "Metaso MiniMax requires an API key",
        )

    if (
        stop_at in {"materials", "video"}
        and params.video_source == "muapi"
        and not muapi.is_enabled()
    ):
        return _mark_task_failed(
            task_id,
            "preflight",
            "MuAPI video generation requires a MuAPI API key",
        )

    if (
        stop_at in {"materials", "video"}
        and params.video_source == "openai_image"
        and not material.is_openai_image_enabled(
            config.snapshot_config_with_pending(config.app)
        )
    ):
        return _mark_task_failed(
            task_id,
            "preflight",
            "OpenAI image source requires openai_image_base_url and "
            "openai_image_model in config.toml (openai_image_api_keys is "
            "optional for local gateways that need no auth)",
        )

    # 只有完整成片流程需要视频配乐供应商。尽早阻止缺少 Key 的完整任务，避免
    # 先消耗 LLM、TTS 和素材服务额度；中间产物接口仍可独立使用。
    video_music_provider = _VIDEO_MUSIC_PROVIDERS.get(params.bgm_type)
    video_music_enabled = (
        stop_at == "video"
        and video_music_provider is not None
        and bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume)
    )
    if video_music_enabled:
        service = video_music_provider["service"]
        display_name = video_music_provider["display_name"]
        if not service.is_enabled():
            return _mark_task_failed(
                task_id,
                "preflight",
                f"{display_name} background music requires an API key",
            )

        # WebUI 会限制输入长度，但 API、CLI 和历史任务可以绕过前端控件。
        # 在生成脚本、配音和素材之前按供应商上限再次校验，避免完整视频合成后
        # 才由第三方请求拒绝。服务层仍保留同一校验，作为直接调用时的最后防线。
        music_prompt = _get_video_music_prompt(params)
        max_prompt_length = int(getattr(service, "MAX_PROMPT_LENGTH", 0) or 0)
        if max_prompt_length and len(music_prompt) > max_prompt_length:
            return _mark_task_failed(
                task_id,
                "preflight",
                (f"{display_name} music prompt exceeds {max_prompt_length} characters"),
            )

        # 供应商可以选择提供不计费的账号前置检查。检查函数只应抛出确定性
        # 错误；网络波动或权限范围无法确认时由服务层记录警告并继续实际生成。
        validate_access = getattr(service, "validate_generation_access", None)
        if callable(validate_access):
            try:
                validate_access()
            except video_music_provider["error_type"] as exc:
                return _mark_task_failed(task_id, "preflight", str(exc))

    # 只有 script/terms 中间产物不需要 FFmpeg（它们不生成音频或视频）。API、
    # CLI 和 WebUI 都通过这个共享入口执行任务，因此在此统一探测，而不是
    # 分别在各个入口重复检查，能保证三条路径的行为一致。放在配乐 Key 校验
    # 之后，是为了不改变那些校验原有的"最先失败"顺序和错误信息。
    if stop_at not in ("script", "terms") and not utils.check_ffmpeg_ready():
        return _mark_task_failed(
            task_id,
            "preflight",
            "ffmpeg is not available; install ffmpeg or set app.ffmpeg_path "
            "in config.toml to a working ffmpeg executable",
        )

    # 配音三件套相容性:空音色回填配置默认值;指向缺 Key 的付费 TTS 要提前拦截。
    # 原实现只在 generate_audio 里失败,用户要等脚本阶段(数十秒)才看到一句
    # 无法定位的 "verify the selected voice and TTS connectivity"。这里提前
    # 到 preflight,秒级返回可操作的错误,并避免浪费 LLM 额度。
    if stop_at in {"audio", "subtitle", "materials", "video"}:
        _normalize_voice_params(params)
        voice_error = _voice_preflight_error(params)
        if voice_error:
            return _mark_task_failed(task_id, "preflight", voice_error)

    # 1. Generate script
    video_script = generate_script(task_id, params)
    # The LLM adapter uses a leading "Error: " as its failure sentinel. A
    # user-provided script may legitimately quote that text anywhere, including
    # at the beginning, so only interpret it for an LLM-generated script.
    is_provider_error = (
        not (params.video_script or "").strip()
        and isinstance(video_script, str)
        and video_script.startswith("Error: ")
    )
    if not video_script or is_provider_error:
        error = (
            video_script.removeprefix("Error: ").strip()
            if is_provider_error
            else "failed to generate video script"
        )
        return _mark_task_failed(task_id, "script", error)

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=10)

    if stop_at == "script":
        sm.state.update_task(
            task_id, state=const.TASK_STATE_COMPLETE, progress=100, script=video_script
        )
        return {"script": video_script}

    # 2. Generate terms
    video_terms = ""
    if stop_at in {"terms", "materials", "video"} and params.video_source != "local":
        video_terms = generate_terms(task_id, params, video_script)
        if not video_terms:
            return _mark_task_failed(
                task_id,
                "terms",
                "failed to generate video search terms",
            )

    save_script_data(task_id, video_script, video_terms, params)

    # Biography fork: the structured script must be written AFTER
    # save_script_data, because that call replaces the whole task file and
    # would otherwise erase these records.
    if isinstance(getattr(params, "biography_script", None), dict):
        try:
            task_artifacts.patch_script_data(
                task_id,
                biography_script=params.biography_script,
                biography_facts_check=params.biography_script.get("facts_check"),
                biography_person=params.biography_person,
            )
        except Exception as exc:
            logger.warning(
                f"failed to persist biography script: {type(exc).__name__}: {exc}"
            )

    # 普通视频分镜在 generate_script 已暂存到 storyboard.json;save_script_data
    # 之后把它合并进 script.json(供作品/任务追溯画面-解说对齐),再删除暂存文件。
    if stop_at != "terms":
        sb = _load_staged_storyboard(task_id)
        if isinstance(sb, dict):
            try:
                task_artifacts.patch_script_data(task_id, storyboard=sb)
            except Exception as exc:
                logger.warning(
                    f"failed to persist storyboard: {type(exc).__name__}: {exc}"
                )
            try:
                os.unlink(_storyboard_stage_path(task_id))
            except OSError:
                pass

    if stop_at == "terms":
        sm.state.update_task(
            task_id, state=const.TASK_STATE_COMPLETE, progress=100, terms=video_terms
        )
        return {"script": video_script, "terms": video_terms}

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=20)

    # 3. Generate audio
    generate_audio_kwargs = {
        "voice_preview": voice_preview,
        "allow_server_file_input": allow_server_file_input,
    }
    if voxcpm_reference_audio is not None:
        generate_audio_kwargs["voxcpm_reference_audio"] = voxcpm_reference_audio
    if voxcpm_prompt_audio is not None:
        generate_audio_kwargs["voxcpm_prompt_audio"] = voxcpm_prompt_audio
        generate_audio_kwargs["voxcpm_prompt_text"] = voxcpm_prompt_text
    audio_file, audio_duration, sub_maker = generate_audio(
        task_id,
        params,
        video_script,
        **generate_audio_kwargs,
    )
    if not audio_file:
        return _mark_task_failed(
            task_id,
            "audio",
            "failed to prepare narration audio",
        )

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=30)

    if stop_at == "audio":
        # Full video jobs apply gain in the final mixer. Audio-only exports have
        # no mixer, so apply it here to an owned output (never a custom input).
        audio_volume = 1.0 if params.voice_volume is None else float(params.voice_volume)
        if audio_volume != 1.0:
            descriptor, output_file = tempfile.mkstemp(
                prefix="audio-export-", suffix=".mp3", dir=utils.task_dir(task_id)
            )
            os.close(descriptor)
            export_ready = False
            try:
                export_ready = voice.apply_audio_volume(audio_file, output_file, audio_volume)
                if not export_ready:
                    return _mark_task_failed(task_id, "audio", "failed to apply audio export volume")
            finally:
                if not export_ready:
                    try:
                        os.remove(output_file)
                    except OSError as exc:
                        logger.warning(f"failed to remove incomplete audio export: {exc}")
            audio_file = output_file
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            audio_file=audio_file,
        )
        return {"audio_file": audio_file, "audio_duration": audio_duration}

    # 4. Generate subtitle
    subtitle_path = generate_subtitle(
        task_id, params, video_script, sub_maker, audio_file
    )

    if stop_at == "subtitle":
        if not subtitle_path:
            return _mark_task_failed(
                task_id,
                "subtitle",
                "failed to generate subtitles; verify the subtitle provider and timeline",
            )
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            subtitle_path=subtitle_path,
        )
        return {"subtitle_path": subtitle_path}

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=40)

    # 5. Get video materials
    downloaded_videos = get_video_materials(
        task_id,
        params,
        video_terms,
        audio_duration,
        loomloom_video_request=loomloom_video_request,
        shot_visual_types=_shot_visual_types(params, video_terms),
    )
    if not downloaded_videos:
        return _mark_task_failed(
            task_id,
            "materials",
            "failed to prepare video materials",
        )

    if stop_at == "materials":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            materials=downloaded_videos,
        )
        return {"materials": downloaded_videos}

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=50)

    # 仅完整视频生成流程才需要处理视频拼接模式；
    # 这样可以避免 /subtitle 和 /audio 这类请求访问不存在的字段。
    if type(params.video_concat_mode) is str:
        params.video_concat_mode = VideoConcatMode(params.video_concat_mode)

    # 6. Generate final videos
    partial_results = {}
    try:
        final_video_paths, combined_video_paths, generation_warnings = generate_final_videos(
            task_id,
            params,
            downloaded_videos,
            audio_file,
            subtitle_path,
            audio_duration,
            partial_results=partial_results,
        )
    except Exception as exc:
        logger.exception(f"video batch failed, task_id: {task_id}, error: {exc}")
        # Warnings for the failing clip do not describe any usable output.
        completed_count = len(partial_results.get("videos", []))
        partial_results["warnings"] = [
            warning for warning in partial_results.get("warnings", [])
            if warning.get("video_index", 0) <= completed_count
        ] or None
        return _mark_task_failed(
            task_id, "video", f"{type(exc).__name__}: {exc}", details=partial_results,
        )

    if not final_video_paths:
        return _mark_task_failed(
            task_id,
            "video",
            "failed to generate final video",
        )

    logger.success(
        f"task {task_id} finished, generated {len(final_video_paths)} videos."
    )

    # 质量闸门:独立复核磁盘上的真实成片(时长/流/分辨率/黑帧),把结论写回
    # script.json 并附加到任务状态。任何探测失败只降级为警告,不把已经渲染
    # 成功的任务改成失败 —— 用户至少能拿到产物,同时看到客观质量结论。
    try:
        from app.services import quality_gate

        quality_reports = []
        # 质量阈值可配置(0=不判定),让部署方可自定义"合格"标准。
        try:
            min_dur = float(config.app.get("quality_min_duration", 0) or 0)
        except (TypeError, ValueError):
            min_dur = 0.0
        try:
            min_w = int(config.app.get("quality_min_width", 0) or 0)
        except (TypeError, ValueError):
            min_w = 0
        for video_path in final_video_paths:
            report = quality_gate.check_video_quality(
                video_path,
                requested_aspect=getattr(params, "video_aspect", "") or "",
                min_duration=min_dur,
                min_width=min_w,
            )
            report["video_path"] = video_path
            quality_reports.append(report)
            if not report.get("passed"):
                logger.warning(
                    f"quality gate flagged {video_path}: {report.get('issues')}"
                )
        if quality_reports:
            try:
                task_artifacts.patch_script_data(
                    task_id, quality_report=quality_reports
                )
            except Exception as exc:
                logger.warning(
                    f"failed to persist quality report: {type(exc).__name__}"
                )
    except Exception as exc:
        logger.warning(
            f"quality gate crashed: {type(exc).__name__}: {exc}"
        )

    # Phase 5: compliance sidecar next to each finished video (source images +
    # licenses + models). Auxiliary: failure never blocks the task result.
    try:
        from app.services import compliance

        sources = _load_material_sources(task_id)
        for video_path in final_video_paths:
            compliance.write_sidecar(
                video_path,
                task_id=task_id,
                person=getattr(params, "biography_person", "") or params.video_subject or "",
                material_sources=sources,
                script=getattr(params, "biography_script", None),
                language=getattr(params, "video_language", "") or "",
            )
    except Exception as exc:
        logger.warning(
            f"failed to write compliance sidecar: {type(exc).__name__}: {exc}"
        )

    # 7. 先完成视频生成任务，再按需提交跨平台发布。第三方上传可能耗时
    # 数分钟，不应阻塞视频结果返回，也不能反向影响已经生成的成片。
    cross_post_enabled = (
        upload_post.upload_post_service.is_configured()
        and upload_post.upload_post_service.auto_upload
    )
    platforms = (
        list(upload_post.upload_post_service.platforms) if cross_post_enabled else []
    )
    should_cross_post = cross_post_enabled and bool(platforms)
    if cross_post_enabled and not platforms:
        logger.warning(
            f"skip cross-post because no platforms are configured, task_id: {task_id}"
        )
    cross_post_state = const.CROSS_POST_STATE_PENDING if should_cross_post else None

    kwargs = {
        "videos": final_video_paths,
        "combined_videos": combined_video_paths,
        "script": video_script,
        "terms": video_terms,
        "audio_file": audio_file,
        "audio_duration": audio_duration,
        "subtitle_path": subtitle_path,
        "materials": downloaded_videos,
        "cross_post_state": cross_post_state,
        "cross_post_results": None,
        "cross_post_error": None,
        "cross_post_owner": _cross_post_process_owner if should_cross_post else None,
        "warnings": generation_warnings or None,
    }
    sm.state.update_task(
        task_id, state=const.TASK_STATE_COMPLETE, progress=100, **kwargs
    )

    if should_cross_post:
        scheduling_error = _schedule_cross_post(
            task_id=task_id,
            video_paths=final_video_paths,
            params=params,
            video_script=video_script,
            platforms=platforms,
            youtube_privacy_status=(
                upload_post.upload_post_service.youtube_privacy_status
            ),
            # 固定排队时的受众选择，之后修改 WebUI 不应改变已排队视频的声明。
            youtube_made_for_kids=(
                upload_post.upload_post_service.youtube_made_for_kids
            ),
        )
        # 队列满或线程池关闭属于同步可知的调度失败。任务状态已经由调度函数
        # 更新，这里同步修正返回快照，避免调用方收到与后续查询不一致的 pending。
        if scheduling_error:
            kwargs["cross_post_state"] = const.CROSS_POST_STATE_FAILED
            kwargs["cross_post_error"] = scheduling_error
            kwargs["cross_post_owner"] = None

    return kwargs


def start(
    task_id,
    params: VideoParams,
    stop_at: str = "video",
    voice_preview: dict | None = None,
    loomloom_video_request: loomloom.LoomLoomConfirmedVideoRequest | None = None,
    allow_server_file_input: bool = False,
    voxcpm_reference_audio: bytes | None = None,
    voxcpm_prompt_audio: bytes | None = None,
    voxcpm_prompt_text: str = "",
):
    """
    执行任务流水线，并确保未预期异常也会转换成可查询的失败状态。

    ``allow_server_file_input`` 只供本机 CLI 使用。HTTP API 和 WebUI 必须保持
    默认值，让自定义音频始终受当前任务目录约束。
    """
    try:
        return _run_pipeline(
            task_id,
            params,
            stop_at=stop_at,
            voice_preview=voice_preview,
            loomloom_video_request=loomloom_video_request,
            allow_server_file_input=allow_server_file_input,
            voxcpm_reference_audio=voxcpm_reference_audio,
            voxcpm_prompt_audio=voxcpm_prompt_audio,
            voxcpm_prompt_text=voxcpm_prompt_text,
        )
    except Exception as exc:
        logger.exception(
            f"unexpected task pipeline failure, task_id: {task_id}, error: {exc}"
        )
        return _mark_task_failed(
            task_id,
            "pipeline",
            f"{type(exc).__name__}: {exc}",
        )


if __name__ == "__main__":
    task_id = "task_id"
    params = VideoParams(
        video_subject="金钱的作用",
        voice_name="zh-CN-XiaoyiNeural-Female",
        voice_rate=1.0,
    )
    start(task_id, params, stop_at="video")
