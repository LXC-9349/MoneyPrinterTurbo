<div align="center">

# MoneyPrinterTurbo · AI 人物传记短视频工作室 💸

### 输入一个人名，产出一支可发布的传记解说短视频

> 基于 [harry0703/MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo)（MIT）二次开发。
> 输入人物名（如「爱因斯坦」「曹操」），即可生成 3–5 分钟传记解说视频：
> **结构化旁白 + AI 配音 + 该人物真实历史照片 + 精确字幕 + BGM + AI 生成标识 + 合规溯源清单**。
> 前端为全新的 **SolidStart** 工作台，后端为 **FastAPI**；二开能力详见
> [docs/BIOGRAPHY_FORK.md](docs/BIOGRAPHY_FORK.md) 与 [docs/PRODUCT_RESTRUCTURE_v2.md](docs/PRODUCT_RESTRUCTURE_v2.md)。

[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Node](https://img.shields.io/badge/node-24%2B-3178C6?logo=node.js&logoColor=white)](https://nodejs.org/)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)](#-快速开始)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

</div>

---

## 这是什么 🎯

本项目是一套**面向单用户、本地部署的 AI 短视频生成工作室**，把「生成 → 质量 → 管理」做成闭环。它在原版 MoneyPrinterTurbo 的成熟引擎之上，重点补强了三件事：

- **开箱即用**：全新的默认配置即可从 WebUI 一键出片；配音/密钥缺失时在提交阶段秒级拦截，并给出可操作的提示。
- **高质量产出**：普通视频也走结构化分镜，成片前经过客观质量闸门（时长/音视频流/分辨率/黑帧）校验。
- **传记模式**：这是与原版最大的差异——从公开档案源抓取人物**本人真实历史照片**，用本地史料库做事实底座、逐镜核对年份地点，并烧录 AI 标识、输出合规 sidecar。

## 核心功能 ✨

### 两种创作模式
- **普通模式**：提供视频主题/关键词，自动完成脚本 → 分镜 → 配音 → 素材 → 字幕 → 配乐的成片流水线，也支持每个环节替换为自定义内容。
- **传记模式**：以「人物」为中心的向导（选人物 → 选剧本 → 设置 → 生成），产出 3–5 分钟结构化传记解说，画面优先使用人物本人的历史照片。

### 四步向导工作台（SolidStart 前端）
所有创作都在统一的向导里推进，编辑态在项目内四步之间切换时不丢失，任务进度实时轮询：

| 步骤 | 说明 |
|------|------|
| **内容** | 主题/脚本、传记人物选择器、LLM 生成脚本与分镜、内容校验门 |
| **视觉** | 画幅（9:16 / 16:9 / 1:1）、素材源、片段时长与拼接/转场 |
| **声音** | TTS 服务商与音色、语速音量、配音试听、BGM |
| **字幕与生成** | 字幕样式、AI 标识、提交生成与进度、成片预览 |

### 真实历史档案素材源（`historical_archive`）
- 免 Key 的公开来源：**Wikimedia Commons**（人物真实历史照片主力）+ **Wikipedia REST**（事实底座与封面人像）+ **Library of Congress**（欧美人物补充）。
- **授权白名单强制**：仅接受 PD / CC0 / CC BY，拒绝会传染授权的 CC BY-SA / NC / ND。
- **检索增强 + 降级链**：中文名自动解析英文条目并拼接朝代英文词；某分镜无档案素材则换同人物其它照片，仍无则整片降级到库存 b-roll；任一来源故障都不中断任务。

### 本地史料库与素材库管理
- 离线流水线（`scripts/library/*`）构建可全文检索的 SQLite 语料库（文章 / 书籍章节 / 人物档案），消费层 `app/services/library.py` 只读、故障即降级；书籍仅作事实来源，单条片段严格限长，绝不向 prompt 输出成段原文。
- **素材库页面**：语料统计 + 「文档检索」「人物管理」两个标签；人物档案支持按朝代/类别/分组筛选、增删改查、置顶/隐藏、备注与关键事实维护。

### 结构化脚本与事实校验
- 脚本输出为严格 JSON：开场钩子 + 逐镜 `timeline`（旁白、时长、视觉类型 `historical_photo|generated_scene|b_roll`、逐镜英文检索词）+ 结尾升华；`visual_type` 直接驱动素材调度。
- 逐镜核对年份/地点，未通过的镜头标记 `verified:false` 并给出原因；语料过薄时降级为「需人工复核」而非误判，不阻断生成。

### 逐镜配音与精确字幕
- 统一 TTS 入口（`app/services/tts_provider.py`），集成 **Edge TTS（免费无需 Key）**、Azure、SiliconFlow、Gemini、MiniMax、ElevenLabs、Fish Audio、VoxCPM（支持上传参考音频做音色复刻）等，并提供无配音/上传配音模式。
- **逐分镜合成 + 拼接**，累加时长即得精确的镜级边界，字幕与画面对应关系由构造保证（无需强制对齐）；faster-whisper 转写为可选的更精确时间轴方案。

### 质量闸门与合规
- 合成后用 ffprobe/ffmpeg 客观校验（时长 > 0、含音视频流、分辨率、全黑帧抽样），结论写入任务产物，不达标降级为质量警告而非静默交付。
- **AI 生成标识**：默认「内容由 AI 生成」烧录式角标（位置/字号/透明度可配），叠加于字幕之上永不被遮挡。
- **合规 sidecar**（`<视频同名>.compliance.json`）：逐图来源页/授权/作者、授权统计、所用 LLM/TTS/字幕模型、待复核镜头清单。

### 项目管理
- 作品列表具备封面（ffmpeg 抽首帧）、状态徽标、画幅/来源/时长/大小、搜索与筛选（模式/状态/人物）、重命名、重新生成、导出、删除（任务目录被删时容错灰显）；生成完成后自动入库。

### 多种使用方式
- **WebUI（SolidStart）**：主力图形界面。
- **API（FastAPI）**：`/api/v1/videos`、`/projects`、`/library/*`、`/settings` 等端点，供自动化接入。
- **CLI**：无浏览器环境下 `python cli.py --video-subject ...`，支持 `--batch-file` 批量清单。

## 技术架构 🧱

```
MoneyPrinterTurbo (fork)
├── app/            FastAPI 后端（服务层 + /api/v1 控制器），端口 8917
│   ├── services/     task/video/voice/llm/biography/storyboard/library/
│   │                 quality_gate/compliance/tts_provider/material… 引擎能力
│   ├── controllers/  v1 路由：videos / projects / library / settings / options…
│   └── models/       schema / llm_provider registry
├── frontend/       SolidStart 前端工作台（默认端口 7310）
│   │               SolidStart v2 + Vite 8 + Nitro + Tailwind v4 + Kobalte
│   └── src/routes/   工作区 / 项目向导 / 素材库 / 设置
├── scripts/        史料库离线流水线 + 开发启动器（dev_serve.py / dev_api.ps1）
└── resource/       字体 / 预设音乐 等静态资源
```

- **进程边界**：SolidStart 前端通过 HTTP 访问 FastAPI（写/轮询走浏览器直连 `PUBLIC_API_BASE`，服务端读代理走 `API_INTERNAL_BASE`）。
- **配置分层**：全局配置（LLM/素材/语音密钥等，写入 `config.toml` 的 `[app]/[ui]`）与项目级 draft 参数（各向导步骤的临时编辑态，存于项目记录）分离。

## 环境要求 📦

- Python 3.11+（推荐 3.11），包管理推荐 [uv](https://docs.astral.sh/uv/)
- Node.js 24+，前端包管理**强制使用 pnpm**
- GPU 非必需；仅在使用本地 Whisper 转录或重度批量处理时能明显提速

| 项目 | 最低 | 推荐 |
| ---- | ---- | ---- |
| CPU  | 4 核 | 6–8 核 |
| RAM  | 4 GB | 8–16 GB |
| GPU  | 非必须 | 4 GB+ 显存 |

## 快速开始 🚀

### 1. 后端（FastAPI，默认端口 8917）

```shell
git clone https://github.com/LXC-9349/MoneyPrinterTurbo.git
cd MoneyPrinterTurbo
uv python install 3.11
uv sync --frozen
# 首次启动会依据 config.example.toml 自动生成 config.toml
uv run python main.py            # 生产方式启动 API
```

本地开发推荐用启动器（自动注入前端 dev 源到 CORS 白名单）：

```shell
# Windows PowerShell
uv run python scripts/dev_serve.py
# 或调用 scripts/dev_api.ps1
```

配置云端大模型、在线素材或 AI 视频服务前，请在「设置 → 全局配置」或 `config.toml` 填写对应 API Key（`Edge TTS` 无需 Key，可零配置出片）。

### 2. 前端（SolidStart，默认端口 7310）

```shell
cd frontend
pnpm install
cp .env.example .env            # 按需调整 PUBLIC_API_BASE / API_INTERNAL_BASE
pnpm dev                        # 打开 http://localhost:7310
```

常用命令：

```shell
pnpm typecheck     # 类型检查
pnpm test          # Vitest 单元测试
pnpm test:e2e      # Playwright 端到端（需后端先行）
pnpm build         # 生产构建，产物在 .output/
node .output/server/index.mjs   # 运行构建产物
```

### 3. CLI / API

```shell
uv run python cli.py --video-subject "人工智能如何改变日常生活"   # 命令行出片
uv run python cli.py --help                                       # 完整参数
```

## 端口与跨域 🔌

- 后端 API：**8917**（`config.toml` 的 `listen_port`）；交互式文档：`http://127.0.0.1:8917/docs`
- 前端工作台：**7310**（`frontend/vite.config.ts`）
- 这些端口**刻意避开 8080/3000 等热门端口**，减少与其它本地服务/端口扫描的冲突。
- API 默认仅允许同源访问；独立前端需从其它来源直连时，通过环境变量 `CORS_ALLOWED_ORIGINS` 配置可信来源（如 `http://localhost:7310`）。`curl`、Postman、服务端调用不受 CORS 限制。

## 产物结构 📂

每个任务在 `storage/tasks/<task_id>/` 下产出可溯源的完整链路：

```
final-1.mp4                 成片
audio.mp3                   配音
subtitle.srt                字幕
script.json                 结构化脚本（含分镜与 quality_report）
final-1.compliance.json     合规 sidecar（逐图授权与模型清单）
```

## 文档索引 📖

- 5 分钟上手：[docs/QUICKSTART.md](docs/QUICKSTART.md)
- 架构与模块地图：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 配置项与环境变量：[docs/CONFIG.md](docs/CONFIG.md)
- 常见问题排查：[docs/FAQ.md](docs/FAQ.md)
- 传记系统完整说明与实测产物：[docs/BIOGRAPHY_FORK.md](docs/BIOGRAPHY_FORK.md)
- 产品诊断与重构方案：[docs/PRODUCT_RESTRUCTURE_v2.md](docs/PRODUCT_RESTRUCTURE_v2.md)
- 各阶段开发日志与 API 示例：[docs/dev-log/](docs/dev-log/)
- 历史过程记录（非权威，仅供追溯）：[docs/archive/](docs/archive/)

## 常见问题 🤔

<details>
<summary>RuntimeError: No ffmpeg exe could be found</summary>

ffmpeg 通常会自动下载并被检测。若网络受限无法自动下载，请从 [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) 下载并在 `config.toml` 设置路径（注意 Windows 分隔符为 `\\`）：

```toml
[app]
ffmpeg_path = "C:\\Users\\you\\Downloads\\ffmpeg.exe"
```

</details>

<details>
<summary>提交任务时在音频阶段秒级失败</summary>

`voice_mode = tts` 时配音三件套需相容：`voice_name` 不能为空、`tts_server` 不能是缺 Key 的付费源。请用免费的 `azure-tts-v1`（Edge TTS）或补齐对应服务商 Key。

</details>

<details>
<summary>Whisper 模型下载失败</summary>

首次使用会尝试从 Hugging Face 下载。网络受限时可手动下载 `faster-whisper-large-v3`，放到 `models/whisper-large-v3`；或在设置中改回 `subtitle_provider = "edge"`（Edge TTS 自带词级时间戳）。

</details>

## 上游与许可 📝

本项目 fork 自 [harry0703/MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo)，沿用其 MIT 许可（见 [LICENSE](LICENSE)），并持续合并上游更新。原版由社区维护、感谢各赞助方支持；本 fork 专注于「AI 人物传记短视频」的本地化生产与质量/合规闭环。
