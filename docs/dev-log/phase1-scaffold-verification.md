# Phase 1 — 原始脚手架验证记录（未改任何业务代码）

- 基线 commit: `552e0ee` (2026-09-16, upstream main)
- 环境: conda env `mpt` (Python 3.12.14)，依赖 `requirements.txt` 全量安装通过
- 网络: 统一走代理 `http://192.168.0.100:1080`（config.toml `[proxy]` 可动态配置，
  本地 LLM 网关加入 NO_PROXY 绕过）
- LLM: 本地 new-api 网关 `http://192.168.0.188:9966/v1`，provider=`oneapi`，
  model=`deepseek-v4-flash:free`（免 key 成本）
- TTS: 内置 edge-tts (`zh-CN-XiaoxiaoNeural`)，实测出音正常
- 素材源: pexels（key 复用 pj_baby/.env，仅写入被 gitignore 的本地 config.toml）

## 服务
- API: `python main.py -api` → http://127.0.0.1:8080 （/ping 返回 "pong"）
- WebUI: `streamlit run webui/Main.py` → http://127.0.0.1:8501 （HTTP 200）

## 端到端 demo
- 任务: `POST /api/v1/videos`，subject="爱因斯坦的三大人生转折"，16:9，pexels
- task_id: `3b4c0652-cf24-466c-b0ea-d7274a866df9`，state=1(成功)，progress=100
- 产物:
  - `storage/tasks/3b4c0652.../final-1.mp4` — h264+aac, 1920x1080, 54.7s, 31MB
  - 同目录 `audio.mp3` / `subtitle.srt` / `script.json`（含 material_sources 溯源）
- 日志: `/tmp/mpt_api.log`（脚本→配音→字幕→按脚本顺序下载 14 条 pexels 素材→合成）

## 已知差距（后续阶段解决）
1. demo 仅 55s：原版脚本 prompt 面向 60s 口播，3-5 分钟传记需要结构化分镜脚本（阶段3）
2. 画面为泛用 pexels 素材，非人物真实照片（阶段2）
3. 字幕时间戳来自 edge-tts SubMaker 词级边界（并非均分，精度可接受；
   强制对齐作为可选 provider 保留，阶段4）
4. 无 AI 内容标识与合规 sidecar（阶段5）
