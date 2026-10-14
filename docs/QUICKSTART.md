# 快速上手（5 分钟）

本项目是**单用户、本地部署**的 AI 人物传记短视频工作室：输入一个人名 → 产出一支
3–5 分钟传记解说短视频。后端 FastAPI，前端 SolidStart。

## 环境要求
- Python 3.11+，包管理用 [uv](https://docs.astral.sh/uv/)
- Node.js 24+，前端包管理**强制 pnpm**
- **ffmpeg**（视频合成必需；缺失时出片阶段会失败，见 [FAQ](FAQ.md)）
- GPU 非必需，仅本地 Whisper 转录 / 重度批量时提速

## 1. 后端（FastAPI，默认端口 8917）
```shell
git clone https://github.com/LXC-9349/MoneyPrinterTurbo.git
cd MoneyPrinterTurbo
uv python install 3.11
uv sync --frozen
# 首次运行会依据 config.example.toml 自动生成 config.toml
uv run python main.py            # http://127.0.0.1:8917/docs
```
本地开发推荐用启动器（自动把前端 dev 源注入 CORS 白名单）：
```shell
uv run python scripts/dev_serve.py     # 或 scripts/dev_api.ps1
```

## 2. 前端（SolidStart，默认端口 7310）
```shell
cd frontend
pnpm install
cp .env.example .env             # 按需调整 PUBLIC_API_BASE / API_INTERNAL_BASE
pnpm dev                         # 打开 http://localhost:7310
```
常用命令：`pnpm typecheck` / `pnpm test` / `pnpm test:e2e`（需后端先行）/ `pnpm build`。

## 3. CLI / API（无浏览器环境）
```shell
uv run python cli.py --video-subject "人工智能如何改变日常生活"
uv run python cli.py --help
```

## 4. Docker

> ⚠️ **Docker 镜像目前仅提供后端 API（8917），不含 Web 界面。** SolidStart 前端尚未容器化，直到阶段 3 完成前，请按下文 §2 本地 `pnpm dev` 运行界面。

```shell
docker compose up -d                                   # 后端 API（8917，无界面）
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d   # GPU
```

## 零配置出片
`Edge TTS`（`tts_provider = "edge"`）无需任何 Key，配好 ffmpeg 即可从界面一键出片。
配置云端大模型、在线素材或 AI 视频服务前，才需要在「设置」或 `config.toml` 填对应 Key。

> 端口、跨域与配置分层的细节见 [ARCHITECTURE.md](ARCHITECTURE.md) 与 [CONFIG.md](CONFIG.md)。
