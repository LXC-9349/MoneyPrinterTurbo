# webui/generation.py
import streamlit as st
import requests
from loguru import logger
from webui.audio import (_get_reusable_full_voice_preview,_render_audio_settings)
from webui.base import (CUSTOM_AUDIO_EXTENSIONS,LOCAL_MATERIAL_EXTENSIONS,VOICE_MODE_UPLOAD,_save_runtime_config,_set_runtime_config,tr)
from webui.scripts import (_create_loomloom_video_backend,_current_loomloom_video_quote_context,_render_script_settings)
from webui.settings import (_render_settings_transfer)
from webui.subtitles import (_render_subtitle_settings)
from webui.tasks import (_add_active_generation_task,_get_unmet_restore_upload_requirements,_open_settings_dialog,_prepare_generation_task,_remove_active_generation_task,_render_base_script_picker,_render_current_generation_task,_render_person_picker)
from webui.video import (_render_video_settings)


import os
from uuid import uuid4
from app.config import config
from app.models.schema import (MaterialInfo)
from app.services import (loomloom,material,metaso_minimax,muapi,ofox,task as tm,voice,volcengine_seedance,webui_task)
from app.services import bgm as bgm_service
from app.services import elevenlabs_music as elevenlabs_music_service
from app.services import sonilo as sonilo_service
from app.utils import utils

def _render_goto_settings_button(branch: str, tab_target: str = "material"):
    """缺密钥时在报错下方给一个直达设置页的按钮。

    服务商密钥已全部下沉到“设置 → 素材接口”，工作区不再内联密钥输入框。
    生成前置校验命中缺密钥时，用一行提示 + 按钮把用户送到能真正解决问题的
    设置页（只保存稳定业务 ID，语言切换后仍能定位），而非展开整段密钥表单。
    每个分支命中后立即 st.stop()，同一次运行只会渲染一个按钮，key 不会冲突。
    """
    st.caption(tr("ws_key_missing_hint"))
    st.button(
        tr("ws_goto_settings"),
        key=f"ws_goto_settings_{branch}",
        icon=":material/settings:",
        on_click=_open_settings_dialog,
        kwargs={"target_tab": tab_target},
    )

def _render_generation_controls(
    params, uploaded_files, uploaded_audio_file, uploaded_bgm_file, voice_mode
):
    """
    校验生成依赖、提交任务，并渲染日志与成片结果。

    返回本次页面执行是否成功提交了新任务。提交前已经请求非阻塞保存，调用方
    据此跳过页面末尾的重复请求。主脚本必须及时结束，定时 Fragment 才能持续
    刷新进度和任务日志。
    """
    restore_upload_requirements = st.session_state.get(
        "task_restore_upload_requirements", {}
    )
    has_local_materials = bool(
        uploaded_files or st.session_state.get("local_video_materials", [])
    )
    has_custom_audio = bool(uploaded_audio_file)
    unmet_restore_requirements = _get_unmet_restore_upload_requirements(
        restore_upload_requirements,
        video_source=params.video_source,
        voice_name=params.voice_name or "",
        has_local_materials=has_local_materials,
        has_custom_audio=has_custom_audio,
        voice_mode=voice_mode,
    )
    if "local_materials" in unmet_restore_requirements:
        st.warning(tr("Task Restore Local Materials Warning"))
    if "custom_audio" in unmet_restore_requirements:
        st.warning(tr("Task Restore Custom Audio Warning"))
    if restore_upload_requirements and not unmet_restore_requirements:
        # 用户已重新上传文件，或主动切换了素材来源/音色。此时历史任务的上传依赖
        # 已经得到明确处理，清除标记，避免后续普通生成继续显示旧提示。
        st.session_state.pop("task_restore_upload_requirements", None)

    _render_settings_transfer(params)

    start_button = st.button(
        tr("Generate Video"),
        width="stretch",
        type="primary",
        key="generate_video_button",
        on_click=_prepare_generation_task,
    )
    if start_button:
        _save_runtime_config()
        task_id = st.session_state.get("pending_generation_task_id") or str(uuid4())
        _add_active_generation_task(
            task_id,
            subject=params.video_subject or params.video_script or task_id,
        )
        if not params.video_subject and not params.video_script:
            _remove_active_generation_task(task_id)
            st.error(tr("Video Script and Subject Cannot Both Be Empty"))
            st.stop()

        if params.video_source not in [
            "pexels",
            "pixabay",
            "coverr",
            "historical_archive",
            "wavespeed",
            "volcengine_seedance",
            "ofox",
            "metaso_minimax",
            "muapi",
            "loomloom",
            "openai_image",
            "local",
        ]:
            _remove_active_generation_task(task_id)
            st.error(tr("Please Select a Valid Video Source"))
            st.stop()

        if params.video_source == "pexels" and not config.app.get(
            "pexels_api_keys", ""
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the Pexels API Key"))
            _render_goto_settings_button("pexels")
            st.stop()

        if params.video_source == "pixabay" and not config.app.get(
            "pixabay_api_keys", ""
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the Pixabay API Key"))
            _render_goto_settings_button("pixabay")
            st.stop()

        if params.video_source == "coverr" and not config.app.get(
            "coverr_api_keys", ""
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the Coverr API Key"))
            _render_goto_settings_button("coverr")
            st.stop()

        if params.video_source == "wavespeed" and not config.app.get(
            "wavespeed_api_keys", ""
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the WaveSpeed API Key"))
            _render_goto_settings_button("wavespeed")
            st.stop()

        if params.video_source == "wavespeed" and not st.session_state.get(
            "wavespeed_confirm_charge", False
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Confirm WaveSpeed Charge Required"))
            st.stop()

        if params.video_source == "volcengine_seedance" and not (
            volcengine_seedance.is_enabled(
                config.snapshot_config_with_pending(config.app)
            )
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the Volcano Engine Ark API Key"))
            _render_goto_settings_button("volcengine_seedance")
            st.stop()

        if params.video_source == "volcengine_seedance" and not st.session_state.get(
            "volcengine_seedance_confirm_charge", False
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Confirm Volcano Engine Seedance Charge Required"))
            st.stop()

        if params.video_source == "ofox" and not (
            ofox.is_enabled(config.snapshot_config_with_pending(config.app))
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the OFox API Key"))
            _render_goto_settings_button("ofox")
            st.stop()

        if params.video_source == "ofox" and not st.session_state.get(
            "ofox_confirm_charge", False
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Confirm OFox Charge Required"))
            st.stop()

        if params.video_source == "metaso_minimax" and not (
            metaso_minimax.is_enabled(
                config.snapshot_config_with_pending(config.app)
            )
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the Metaso MiniMax API Key"))
            _render_goto_settings_button("metaso_minimax")
            st.stop()

        if params.video_source == "metaso_minimax" and not st.session_state.get(
            "metaso_minimax_confirm_charge", False
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Confirm Metaso MiniMax Charge Required"))
            st.stop()

        if params.video_source == "muapi" and not (
            muapi.is_enabled(config.snapshot_config_with_pending(config.app))
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the MuAPI API Key"))
            _render_goto_settings_button("muapi")
            st.stop()

        if params.video_source == "muapi" and not st.session_state.get(
            "muapi_confirm_charge", False
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Confirm MuAPI Charge Required"))
            st.stop()

        if params.video_source == "openai_image" and not material.is_openai_image_enabled(
            config.snapshot_config_with_pending(config.app)
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Configure the OpenAI Image Source"))
            _render_goto_settings_button("openai_image")
            st.stop()

        loomloom_video_request = None
        if params.video_source == "loomloom":
            current_batch, current_signature = _current_loomloom_video_quote_context(
                params
            )
            quoted_batch = st.session_state.get("loomloom_video_batch")
            quote_result = st.session_state.get("loomloom_video_quote")
            quote_is_current = bool(
                current_batch is not None
                and isinstance(quoted_batch, loomloom.LoomLoomVideoBatch)
                and quote_result is not None
                and st.session_state.get("loomloom_video_input_signature")
                == current_signature
            )
            if not quote_is_current:
                _remove_active_generation_task(task_id)
                st.error(tr("AI Video Quote Required"))
                st.stop()
            if not st.session_state.get("loomloom_video_confirm_charge", False):
                _remove_active_generation_task(task_id)
                st.error(tr("Confirm AI Video Charge Required"))
                st.stop()
            try:
                video_backend = _create_loomloom_video_backend()
                loomloom_video_request = loomloom.LoomLoomConfirmedVideoRequest(
                    settings=video_backend.settings,
                    batch=current_batch,
                    listing_version_id=quote_result.listing_version_id,
                    client_request_id=st.session_state[
                        "loomloom_video_client_request_id"
                    ],
                )
                loomloom_video_request.validate()
            except (loomloom.LoomLoomError, ValueError) as exc:
                _remove_active_generation_task(task_id)
                st.error(str(exc))
                st.stop()

        if (
            params.bgm_type == "sonilo"
            and bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume)
            and not sonilo_service.is_enabled()
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Sonilo API Key Required"))
            st.stop()

        if (
            params.bgm_type == "elevenlabs"
            and bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume)
            and not elevenlabs_music_service.is_enabled()
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("ElevenLabs API Key Required"))
            st.stop()

        if params.video_source == "local" and not has_local_materials:
            # 本地素材为空时继续执行会先产生 TTS/字幕，最后才在素材预处理阶段失败。
            # 在任务启动前拦截，可以避免无意义的 API 调用和中间文件。
            _remove_active_generation_task(task_id)
            st.error(tr("Please Upload Local Materials First"))
            st.stop()

        if voice_mode == VOICE_MODE_UPLOAD and not uploaded_audio_file:
            # 上传音频是用户显式选择的配音方式，缺少文件时不能静默退回 TTS。
            # 在任务启动前拦截，避免产生与用户选择不一致的成片。
            _remove_active_generation_task(task_id)
            st.error(tr("Please Upload Voiceover File First"))
            st.stop()

        if "custom_audio" in unmet_restore_requirements:
            # 历史自定义音频不能自动回填。用户尚未重新上传且也没有主动更换音色时，
            # 必须阻止静默退回 TTS，否则重新生成的结果会与原任务语音不一致。
            _remove_active_generation_task(task_id)
            st.error(tr("Task Restore Custom Audio Warning"))
            st.stop()

        if uploaded_bgm_file and bgm_service.should_use_bgm(
            params.bgm_type, params.bgm_volume
        ):
            try:
                saved_bgm_name = bgm_service.save_bgm_upload(
                    uploaded_bgm_file.name, uploaded_bgm_file
                )
            except bgm_service.BgmUploadError as exc:
                _remove_active_generation_task(task_id)
                logger.warning(f"WebUI background music upload rejected: {str(exc)}")
                st.error(tr("Invalid Background Music"))
                st.stop()
            except bgm_service.BgmServiceError as exc:
                _remove_active_generation_task(task_id)
                logger.error(f"WebUI background music upload failed: {str(exc)}")
                st.error(tr("Background Music Validation Failed"))
                st.stop()
            # 保存成功后只把文件名写入任务参数。视频服务会在两个 BGM 白名单
            # 目录中重新解析，避免把服务器绝对路径持久化或展示给用户。
            params.bgm_file = saved_bgm_name
        elif uploaded_bgm_file:
            # 0 音量时视频服务不会使用任何 BGM，因此不再把已经预览的上传文件
            # 持久化到 storage。用户之后调高音量时可直接再次点击生成完成保存。
            params.bgm_file = ""

        if uploaded_audio_file:
            task_dir = utils.task_dir(task_id)
            try:
                custom_audio_path = _build_uploaded_file_path(
                    uploaded_audio_file,
                    task_dir,
                    CUSTOM_AUDIO_EXTENSIONS,
                    "custom-audio",
                )
            except ValueError:
                _remove_active_generation_task(task_id)
                st.error(tr("Unsupported Upload File Type"))
                st.stop()
            with open(custom_audio_path, "wb") as f:
                f.write(uploaded_audio_file.getbuffer())
            params.custom_audio_file = custom_audio_path

        if uploaded_files:
            local_videos_dir = utils.storage_dir("local_videos", create=True)
            # 每次重新上传时都以本次选择的素材为准，避免旧素材不断重复追加。
            params.video_materials = []
            persisted_local_materials = []
            for file in uploaded_files:
                try:
                    file_path = _build_uploaded_file_path(
                        file,
                        local_videos_dir,
                        LOCAL_MATERIAL_EXTENSIONS,
                        "material",
                    )
                except ValueError:
                    _remove_active_generation_task(task_id)
                    st.error(tr("Unsupported Upload File Type"))
                    st.stop()
                with open(file_path, "wb") as f:
                    f.write(file.getbuffer())
                    m = MaterialInfo()
                    m.provider = "local"
                    m.url = file_path
                    params.video_materials.append(m)
                    persisted_local_materials.append(
                        {
                            "provider": m.provider,
                            "url": m.url,
                            "duration": m.duration,
                        }
                    )
            # 将已上传并保存到本地的视频素材写入会话，供后续只改文案时直接复用。
            st.session_state["local_video_materials"] = persisted_local_materials
        elif (
            params.video_source == "local" and st.session_state["local_video_materials"]
        ):
            # 当用户没有重新上传文件时，复用最近一次已经保存到磁盘的本地素材列表。
            params.video_materials = []
            for material_entry in st.session_state["local_video_materials"]:
                m = MaterialInfo()
                m.provider = material_entry.get("provider", "local")
                m.url = material_entry.get("url", "")
                m.duration = material_entry.get("duration", 0)
                if m.url:
                    params.video_materials.append(m)

        reusable_voice_preview = _get_reusable_full_voice_preview(
            params,
            voice_mode,
        )
        if reusable_voice_preview:
            # 试听缓存只存在当前 Streamlit 会话。提交前把音频写入目标任务目录，
            # 后台线程随后只读取任务自己的文件；即使页面 rerun、浏览器关闭或
            # 用户试听其它音色，也不会影响已经入队的生成任务。
            preview_audio_file = os.path.join(
                utils.task_dir(task_id),
                "audio.mp3",
            )
            with open(preview_audio_file, "wb") as file:
                file.write(reusable_voice_preview.pop("audio_bytes"))
            reusable_voice_preview["audio_file"] = preview_audio_file
            logger.info(
                f"reuse full voice preview for task: "
                f"task_id={task_id}, duration={reusable_voice_preview['duration']:.2f}s"
            )

        try:
            st.toast(tr("Generating Video"))
            logger.info(tr("Start Generating Video"))
            logger.info(utils.to_json(params))
            webui_task.submit_generation(
                task_id=task_id,
                params=params,
                capture_logs=not config.ui.get("hide_log", False),
                voice_preview=reusable_voice_preview,
                loomloom_video_request=loomloom_video_request,
            )
            if loomloom_video_request is not None:
                # 一个报价只允许提交一次。后台请求自带稳定幂等 ID；提交成功后
                # 清除页面报价，下一次生成必须重新询价和确认。
                st.session_state["loomloom_video_batch"] = None
                st.session_state["loomloom_video_quote"] = None
                st.session_state["loomloom_video_input_signature"] = ""
                st.session_state["loomloom_video_client_request_id"] = ""
        except Exception:
            _remove_active_generation_task(task_id)
            st.error(tr("Video Generation Failed"))
            st.stop()

        st.session_state["current_generation_task_id"] = task_id
        logger.info(f"WebUI generation task submitted: task_id={task_id}")

    _render_current_generation_task()
    return start_button

def _biography_wizard_enabled() -> bool:
    """是否进入传记向导：当前素材源为历史档案且本地史料库可用。

    视频面板在向导之后渲染，故从已保存配置读 video_source；史料库不可用
    时降级回普通四栏布局（向导的第一步本身也会给出构建提示）。
    """
    if str(config.app.get("video_source", "")) != "historical_archive":
        return False
    try:
        from app.services import library

        return bool(
            library.enabled()
            and library.db_path() is not None
            and library.get_conn() is not None
        )
    except Exception:  # pragma: no cover - 导入/连接失败降级到普通布局
        return False

def _render_biography_wizard(params):
    """传记模式四步向导：①选人物 ②基础脚本+文案 ③视频/音频/字幕 ④生成。

    用 st.tabs 做分步展示：Streamlit 每次 run 都会执行所有 tab 体内的控件，
    因此各步的 params 与生成所需返回值到第④步时已全部就绪，不会像自建
    step 机那样“只显示当前步”导致跳步时丢参数。
    """
    st.caption(tr("Bio Wizard Intro"))
    tab_person, tab_script, tab_settings, tab_generate = st.tabs(
        [
            tr("Bio Step 1 Person"),
            tr("Bio Step 2 Script"),
            tr("Bio Step 3 Settings"),
            tr("Bio Step 4 Generate"),
        ]
    )

    # ① 选人物：搜索/筛选/档案/人物管理，选中即写入 biography_person。
    with tab_person:
        chosen = _render_person_picker(params.biography_person)
        if not chosen:
            chosen = str(config.app.get("biography_person", "") or "").strip()
        params.biography_person = chosen
        _set_runtime_config("app", "biography_person", chosen)

    # ② 基础脚本（默认全选）+ 生成文案。先定 doc_ids 再进文案面板，使传记
    #    分流生成按钮能拿到本次勾选；已生成的结构化脚本一致时直接复用。
    with tab_script:
        person = params.biography_person
        if person:
            picked = _render_base_script_picker(person)
            # 勾选结果不能放进 params（VideoParams 无该字段，pydantic 会抛
            # "object has no field 'biography_doc_ids'"）。改为存会话态，文案
            # 生成器 _extra_context_from_picked_docs 在生成时读取并拼正文。
            st.session_state[f"biography_picked_doc_ids_{person.strip()}"] = picked
            saved_script = st.session_state.get("biography_script")
            # 已生成的结构化脚本：同传主、同目标语言时直接复用，避免重复
            # 调用大模型。传主/语言变了才重新生成。
            if (
                isinstance(saved_script, dict)
                and saved_script.get("person") == person
                and saved_script.get("language", "") == (params.video_language or "")
            ):
                params.biography_script = saved_script
        _render_script_settings(tab_script, params)

    # ③ 视频/音频/字幕：复用现有面板渲染器（不改内部），在同一 run 内回传值。
    # 传记模式素材源固定为历史档案，故不显示"视频来源"下拉。
    with tab_settings:
        col_video, col_audio, col_subtitle = st.columns(3)
        uploaded_files = _render_video_settings(col_video, params, False)
        uploaded_audio_file, uploaded_bgm_file, voice_mode = _render_audio_settings(
            col_audio, params
        )
        _render_subtitle_settings(col_subtitle, params)

    # ④ 生成：③ 在 ④ 之前执行，返回值本 run 可用。
    with tab_generate:
        submitted = _render_generation_controls(
            params,
            uploaded_files,
            uploaded_audio_file,
            uploaded_bgm_file,
            voice_mode,
        )
    return submitted


# ── functions recovered from the pre-split monolithic Main.py ──────────────
def _build_uploaded_file_path(uploaded_file, target_dir, allowed_extensions, prefix):
    """为浏览器上传文件生成受控的服务端保存路径。"""
    original_name = os.path.basename(str(uploaded_file.name or ""))
    extension = os.path.splitext(original_name)[1].lower()
    if extension not in allowed_extensions:
        logger.warning(
            f"reject unsupported uploaded file extension: {original_name or '<empty>'}"
        )
        raise ValueError("unsupported uploaded file type")

    normalized_target_dir = os.path.realpath(target_dir)
    os.makedirs(normalized_target_dir, exist_ok=True)
    # 不复用浏览器传入的文件名，避免路径分隔符、控制字符或同名覆盖。UUID 只用于
    # 服务端落盘，不改变用户在上传控件中看到的原始名称。
    file_path = os.path.realpath(
        os.path.join(normalized_target_dir, f"{prefix}-{uuid4().hex}{extension}")
    )
    if os.path.commonpath([normalized_target_dir, file_path]) != normalized_target_dir:
        logger.warning(f"invalid uploaded file path: {file_path}")
        raise ValueError("invalid uploaded file path")
    return file_path

