"""全局设置控制器（读写 config.app / config.ui，落盘 config.toml）。

覆盖“能跑起来”所必需的全局配置子集：LLM Provider（Key/BaseURL/模型名）、
素材 API Key、界面日志开关、Upload-Post 自动发布。刻意不复刻 Streamlit 设置
弹窗中 Provider 专属的服务区域状态机、Groq 动态模型列表、密钥备份/恢复等
边缘逻辑——这些留在 WebUI，不属于前端向导切片的必需能力。

Secret 字段按原值读写（仅本地开发，无对外部署），与 config.toml 的暴露面一致。
"""
from fastapi import Depends, Request

from app.config import config
from app.controllers import base
from app.controllers.v1.base import new_router
from app.models.llm_provider import (
    DEFAULT_LLM_PROVIDER_ID,
    LLM_PROVIDER_REGISTRY,
    get_llm_provider,
    normalize_provider_override,
)
from app.models.schema import BaseResponse, SettingsUpdateRequest
from app.services import llm
from app.utils import utils

router = new_router(dependencies=[Depends(base.verify_token)])


def _material_keys(key: str) -> str:
    val = config.app.get(key, [])
    if isinstance(val, str):
        val = [val]
    return ", ".join(val or [])


def _provider_meta(spec) -> dict:
    return {
        "id": spec.provider_id,
        "label": spec.default_label,
        "show_api_key": spec.show_api_key,
        "show_base_url": spec.show_base_url,
        "requires_model_name": spec.requires_model_name,
        "requires_api_key": spec.requires_api_key,
        "default_model": spec.default_model,
        "default_base_url": spec.effective_default_base_url,
        "api_key_url": spec.effective_api_key_url(),
        "model_docs_url": spec.effective_model_docs_url(),
        "service_endpoints": [
            {
                "id": ep.endpoint_id,
                "label": ep.default_label,
                "base_url": ep.base_url,
                "api_key_url": ep.api_key_url,
            }
            for ep in spec.service_endpoints
        ],
    }


@router.get("/settings", response_model=BaseResponse, summary="Read global settings")
def get_settings(request: Request):
    provider = str(config.app.get("llm_provider", DEFAULT_LLM_PROVIDER_ID)).lower()
    spec = get_llm_provider(provider)
    llm_view: dict = {
        "provider": provider,
        "api_key": "",
        "base_url_override": "",
        "base_url_effective": "",
        "model_override": "",
        "model_effective": "",
    }
    if spec is not None:
        api_key = str(config.app.get(spec.config_key("api_key"), "") or "")
        configured_base = str(config.app.get(spec.config_key("base_url"), "") or "")
        configured_model = str(config.app.get(spec.config_key("model_name"), "") or "")
        llm_view.update(
            {
                "api_key": api_key,
                "base_url_override": configured_base,
                "base_url_effective": spec.resolve_base_url(configured_base),
                "model_override": configured_model,
                "model_effective": spec.resolve_model_name(configured_model),
            }
        )

    return utils.get_response(
        200,
        {
            "llm": llm_view,
            "llm_providers": [_provider_meta(s) for s in LLM_PROVIDER_REGISTRY],
            "material": {
                "pexels_api_keys": _material_keys("pexels_api_keys"),
                "pixabay_api_keys": _material_keys("pixabay_api_keys"),
                "coverr_api_keys": _material_keys("coverr_api_keys"),
            },
            "ui": {"hide_log": bool(config.ui.get("hide_log", False))},
            "api_key": str(config.app.get("api_key", "") or ""),
            "upload_post": {
                "enabled": bool(config.app.get("upload_post_enabled", False)),
                "auto_upload": bool(config.app.get("upload_post_auto_upload", False)),
                "api_key": str(config.app.get("upload_post_api_key", "") or ""),
                "username": str(config.app.get("upload_post_username", "") or ""),
                "platforms": list(
                    config.app.get("upload_post_platforms", ["tiktok", "instagram"]) or []
                ),
                "youtube_privacy_status": str(
                    config.app.get("upload_post_youtube_privacy_status", "public") or "public"
                ),
                "youtube_made_for_kids": bool(
                    config.app.get("upload_post_youtube_made_for_kids", False)
                ),
            },
        },
    )


@router.put("/settings", response_model=BaseResponse, summary="Update global settings")
def update_settings(request: Request, body: SettingsUpdateRequest):
    # 目标 Provider：显式切换则用新值，否则沿用当前配置的 Provider。
    target_provider = (
        body.llm_provider.strip().lower()
        if body.llm_provider
        else str(config.app.get("llm_provider", DEFAULT_LLM_PROVIDER_ID)).lower()
    )
    spec = get_llm_provider(target_provider)

    if body.llm_provider:
        config.app["llm_provider"] = target_provider

    if spec is not None:
        if body.llm_api_key is not None:
            config.app[spec.config_key("api_key")] = body.llm_api_key.strip()
        if body.llm_base_url is not None:
            config.app[spec.config_key("base_url")] = normalize_provider_override(
                body.llm_base_url, spec.effective_default_base_url
            )
        if body.llm_model_name is not None:
            config.app[spec.config_key("model_name")] = normalize_provider_override(
                body.llm_model_name, spec.default_model
            )

    for field, key in (
        ("pexels_api_keys", "pexels_api_keys"),
        ("pixabay_api_keys", "pixabay_api_keys"),
        ("coverr_api_keys", "coverr_api_keys"),
    ):
        value = getattr(body, field)
        if value is not None:
            normalized = value.replace(" ", "")
            config.app[key] = normalized.split(",") if normalized else []

    if body.api_key is not None:
        config.app["api_key"] = body.api_key.strip()
    if body.hide_log is not None:
        config.ui["hide_log"] = body.hide_log

    upload_map = {
        "upload_post_enabled": body.upload_post_enabled,
        "upload_post_auto_upload": body.upload_post_auto_upload,
        "upload_post_api_key": body.upload_post_api_key,
        "upload_post_username": body.upload_post_username,
        "upload_post_platforms": body.upload_post_platforms,
        "upload_post_youtube_privacy_status": body.upload_post_youtube_privacy_status,
        "upload_post_youtube_made_for_kids": body.upload_post_youtube_made_for_kids,
    }
    for key, value in upload_map.items():
        if value is not None:
            config.app[key] = value.strip() if isinstance(value, str) else value

    config.save_config()
    return get_settings(request)


@router.post(
    "/settings/llm/test", response_model=BaseResponse, summary="Test LLM connection"
)
def test_llm_connection(request: Request):
    ok, error, elapsed = llm.test_connection()
    return utils.get_response(
        200, {"ok": bool(ok), "error": error or "", "elapsed": float(elapsed or 0.0)}
    )
