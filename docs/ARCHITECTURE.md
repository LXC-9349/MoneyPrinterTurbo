# 架构与模块地图

## 进程边界
```
MoneyPrinterTurbo (fork)
├── app/            FastAPI 后端（默认端口 8917）
│   ├── asgi.py       应用装配：路由、CORS、任务产物静态挂载
│   ├── router.py     根 APIRouter，聚合各控制器
│   ├── controllers/  v1 HTTP 路由（薄），前缀 /api/v1
│   ├── services/     业务编排 + 引擎能力（task/video/voice/llm/biography/…）
│   ├── models/       schema（pydantic）+ llm_provider Registry
│   ├── config/       config.app / config.ui 同步对象，读写 config.toml
│   └── utils/        路径解析、ffmpeg 探测、字幕读写、日志
├── frontend/       SolidStart 工作台（默认端口 7310）
│   └── src/routes/  projects（向导）/ library（素材库·人物）/ settings（设置·存储）
├── scripts/        史料库离线流水线 + 开发启动器 + 示例脚本
├── resource/       字体 / 预设音乐 / 静态公共资源
└── storage/        运行期产物：tasks/ projects/ library/ cache_* 等（被 .gitignore）
```

## 数据流（一次传记出片）
1. 前端向导 PATCH 项目 draft（`/api/v1/projects/{id}`）→ 后端 `project_store`。
2. 提交生成 `POST /api/v1/videos` → `services/task` 状态机排队/运行。
3. 脚本：`services/biography`（结构化 timeline + 逐镜英文检索词 + `verified`）；
   可选 `services/library` 史料 grounding。
4. 素材：`services/material` 按 `video_source` 调度；`historical_archive`
   （`services/archives/*`）取人物真实照片，授权白名单仅 PD/CC0/CC BY。
5. 配音字幕：`services/tts_provider` seam → `services/voice`（Edge 等）；
   `services/shot_audio` 逐镜合成拼接，镜级时长累加即得精确字幕时间轴。
6. 合成：`services/video`（moviepy/ffmpeg）；`services/quality_gate` 客观校验；
   `services/compliance` 烧录 AI 标识并写 `<video>.compliance.json`。
7. 产物落 `storage/tasks/<task_id>/`，前端轮询 `GET /api/v1/tasks/{id}`。

## 前端 ↔ 后端约定
- **写 / 生成 / 轮询 / 上传 / 试听**：浏览器直连 `PUBLIC_API_BASE`（`frontend/src/lib/api/client.ts`）。
- **SSR 读**：`frontend/src/server/queries.ts` 用 `API_INTERNAL_BASE` 服务器对服务器访问，天然同源、免 CORS。
- 统一响应信封 `{ status, message, data }`，HTTP 非 2xx 或 `status >= 400` 抛 `ApiError`。

## 依赖方向（治理目标）
`api → services → engines（TTS/素材/LLM/渲染，可插拔）→ storage`。
禁止控制器/前端直接依赖引擎细节。传记子系统（library/person_store/person_facts）
当前存在函数内延迟 import 规避的双向耦合，**阶段 2 将解为单向并统一到人物仓储**。

## 配置分层
- **全局配置**：`config.toml` 的 `[app]/[ui]`，LLM/素材/语音密钥等；读写走
  `app.config.config.save_config()`（锁保护），设置页 `PUT /api/v1/settings`。
- **项目级 draft**：各向导步骤的临时编辑态，存于项目记录，不写全局 toml。

## 遗留
`webui/`（Streamlit）为原版图形界面，进程内直调 `app.services.*`，能力已由 SolidStart
前端 + `/api/v1` 端点覆盖，正在退役（见重构计划）。史料库（`scripts/library/*`）定位为
**可选增强**，缺失时优雅降级。
