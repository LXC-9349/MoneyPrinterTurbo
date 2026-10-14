# 配置说明

## 单一配置模板
本仓库**只维护一份配置模板** `config.example.toml`。首次运行时后端会把它复制为
`config.toml`（已被 `.gitignore`），所有真实密钥只写在本地 `config.toml`，切勿提交。
> 历史上并存的 `config.optimized.toml` / `config.toml.bak` 已删除；其中含失效语法、
> 误提交的 API Key 和与代码冲突的取值，不作为默认值来源。

## 顶层（API 服务）
| 键 | 默认 | 说明 |
|---|---|---|
| `log_level` | `DEBUG` | DEBUG/INFO/WARNING/ERROR |
| `listen_host` | `0.0.0.0` | 仅本机访问用 `127.0.0.1` |
| `listen_port` | `8917` | 刻意避开 8080/3000 等热门端口 |

## `[app]`（全局业务配置）
按能力分组，值可被设置页 `PUT /api/v1/settings` 覆盖并回写：
- **通用**：`hide_config`、`script_generation_backend`（`local`/`loomloom`）、`edge_tts_timeout`、`tls_verify`
- **LLM**：`llm_provider` + 各提供商 `*_api_key` / `*_base_url` / `*_model_name`
  （三键按 `app/models/llm_provider.py` Registry 归一化，命中默认值时存空串）
- **素材源**：`video_source`（pexels/pixabay/coverr/historical_archive/local/…）、
  `pexels_api_keys` 等 **以 `list[str]` 存储**（前端用逗号分隔文本，写入前归一化）、
  各 AI 视频源 Key 与模型/时长参数
- **历史档案**：`historical_archive_enabled`、`historical_archive_min_image_width`、
  `wikimedia_user_agent`、`material_fallback_source`、`library_enabled`
- **配音**：`tts_provider`（`edge`/`cosyvoice2`，见 `services/tts_provider.py`）
- **合规**：`ai_content_label_enabled/_text/_position/_font_size/_opacity`
- **发布**：`upload_post_*`（可选，多平台自动发布）

## `[ui]`（界面默认偏好）
画幅、拼接模式、转场、字幕样式、音色等，作为新项目 draft 的初始默认；
实际编辑态存项目 draft，不回写全局。

## 环境变量（进程级，优先于/补充 toml）
| 变量 | 用途 |
|---|---|
| `CORS_ALLOWED_ORIGINS` | 允许独立前端跨域直连的来源白名单（如 `http://localhost:7310`） |
| `IMAGEIO_FFMPEG_EXE` | 显式指定 ffmpeg 可执行文件 |
| `REDIS_HOST` / `REDIS_PORT` … | Redis 任务队列（可选，批量场景） |
| `MPT_LIBRARY_DB` / `MPT_PERSONAL_DB` | 覆盖史料库 / 人物库 SQLite 路径 |
| `MPT_STORYBOARD_SECONDS` | 分镜目标时长（默认 45） |
| `WIKIMEDIA_USER_AGENT` | Wikimedia 要求的标识 UA |
| `ELEVENLABS_API_KEY` / `FISH_API_KEY` / `MINIMAX_API_KEY` / `METASO_MINIMAX_API_KEY` / `MUAPI_API_KEY` / `OFOX_API_KEY` / `VOLCENGINE_ARK_API_KEY` | 各服务商 Key 的环境兜底（未填 toml 时读取） |

## 端口与跨域
- 后端 **8917**，前端 **7310**（`frontend/vite.config.ts`）。
- API 默认仅同源；独立前端浏览器直连需 `CORS_ALLOWED_ORIGINS`。`curl`/Postman/服务端调用不受 CORS 限制。
- 前端两个基址：`PUBLIC_API_BASE`（浏览器直连写/轮询）、`API_INTERNAL_BASE`（SSR 读代理，默认 `http://127.0.0.1:8917`），见 `frontend/.env.example`。

## 阶段 2 规划
上述 toml 手改键将收敛为**单一 pydantic settings 模型 + 校验**，界面完成全部管理动作；
`voice_mode`/`tts_server`/`voice_name`「三件套」由后端自动推导合法组合，对外只暴露「音色」。
