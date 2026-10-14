# MoneyPrinterTurbo 重新设计方案 v1

> 状态：**待你确认**。确认 P0 范围与数据模型后开始落地。
> 定位：单用户本地 AI 视频工作室（人物传记 / 口播）。前端继续 Streamlit（方案 A）。
> 原则：零后端破坏；模块拆分只动 webui/；Projects 新增不碰现有任务引擎。

---

## 0. 现状与差距（基于事实）

| 维度 | 现状 | 成熟产品 | 差距 |
|------|------|----------|------|
| 前端 | `webui/Main.py` 单文件 **8569 行** | 组件化 SPA | 无组件、整页 rerun、无流畅预览/拖拽 |
| 用户流 | 生成↔发布割裂；`upload_post` 孤立模块 | 创意→生成→预览→**一键发布** | 发布闭环缺失 |
| 作品管理 | 无；只有 Tasks(管理面板) 存 task 目录 | Projects / 作品库 | 无作品概念（标题/封面/历史/复用） |
| 模板 | 只有 Settings Preset（配置预设） | 产品化模板库（风格+示例成片） | 无模板 |
| 实时体验 | 长任务仅 spinner | 分步进度 + 日志流 + 生成中预览 | 弱 |
| 多端 | 桌面 Web only | 响应式/移动 | 无 |

**已具备（必须保留）**：FastAPI + 30+ service、完整素材链（库存/AI/本地/历史档案）、合规溯源、73 测试+CI、多语言 i18n、多端发布 API、历史档案人物库（1173 人）。

---

## 1. 信息架构（IA）

### 导航（左侧）
```
Create  新建/生成视频   ← 主入口（向导）
Projects 作品          ← 新增：作品库（P0 重点）
Tasks   任务          ← 现有：运行/失败任务监控（保留，偏"运维"）
Storage 存储          ← 现有
Library 资料库
Persons 人物          ← 现有
Settings 设置
```

### 生成向导（统一主线，普通+传记共用）
```
① 创意    选模式(普通/传记) · 主题/人物 · 语言 · 画幅
② 脚本    生成/编辑文案 · 关键词 · 章节
③ 画面    素材源 · 剪辑/转场 · 字幕(字体/位置/动画)
④ 声音    音色 · 语速 · BGM
⑤ 成片    生成按钮 · 实时进度/日志 · 生成中预览
```
> 普通视频与传记视频**走同一条 ①→⑤**；差异只在①②步的内容（主题 vs 人物+档案）。
> 这就是为什么现在"Generation Mode"是补丁式加 dropdown——IA 层没统一。

---

## 2. 前端模块拆分（方案 A）

把 `webui/Main.py`(8569 行) 拆成 ~6 个模块 + 保留 `mpt_admin.py`。

```
webui/
├── __init__.py
├── main.py            # 入口：_render_application 编排 + app 挂载 (≈200 行)
├── generate.py        # 生成向导编排：模式切换、params 初始化、步骤调度 (≈400 行)
├── panels.py          # 四面板+生成控件：script/video/audio/subtitle/generation (≈1800 行)
├── steps.py           # 传记步骤①②③④：人物选择器、基础脚本选择器等 (≈600 行)
├── restore.py         # 恢复流：restore dialog / apply_params / 设置预设导入 (≈600 行)
├── topbar.py          # 侧边栏、品牌、顶部栏、设置入口 (≈300 行)
├── projects.py        # Projects 页面 + 数据访问 (P0)
├── design.py          # 设计 tokens(色值/间距/按钮组件) —— 从 styles.css 抽出
├── i18n/              # 保留
└── mpt_admin.py       # 现有管理面板(tasks/storage/library/persons) 基本不动
```

**拆分原则**：模块间只传 `params`(VideoParams) 和 session_state；不互相 import 页面逻辑；`main.py` 是唯一入口，其它模块都是纯函数/纯渲染。
**验证方式**：拆完跑 `python -m pytest test -q` 确认 73 个测试仍全绿 + Playwright 冒烟两条路径。

---

## 3. P0 功能：Projects（作品/项目管理）

### 3.1 数据模型（推荐：轻量索引，不复制重文件）

不复制 `final-1.mp4`、不复制 `script.json`（已有）。Projects = 一个**命名的索引项**，指向已有任务目录 + 用户起的标题。

`storage/projects/projects.json`（顶层单文件索引）：
```json
{
  "projects": [
    {
      "id": "p_9f2a...",
      "title": "爱因斯坦 · 相对论",        // 用户可编辑；默认=人物+主题
      "description": "",
      "mode": "biography",                  // normal | biography
      "person": "爱因斯坦",
      "subject": "爱因斯坦",
      "language": "zh-CN",
      "aspect": "portrait",                 // 来自 params.video_aspect
      "source": "local",                    // 来自 params.video_source
      "task_id": "10589a05-...",           // 指向 storage/tasks/<id>
      "status": "done",                     // done | running | failed
      "created_at": 1758600000,
      "updated_at": 1758600000,
      "thumbnail": "p_9f2a_01.jpg",         // 可选：从 mp4 抽首帧
      "tags": ["传记", "科学"]
    }
  ]
}
```
- 一个 project 绑定一个 task（生成一次=一个项目）。
- 想"复用/重新生成"→ 复用该 project 的 params 重新跑 → 生成新 task → 可新建 project 或覆盖。

**读取**：`projects.json` 存索引；具体字段从 `storage/tasks/<task_id>/script.json` + 实时 `state.get_task()`（状态/进度）合并。
**写入**：`task_artifacts` 风格的原子 JSON 写；新建 project 时读 `script.json.params` 固化快照（避免后续参数漂移）。

### 3.2 页面（`projects.py`，接进侧边栏 `Projects`）

**列表区**（默认视图）：
- 顶部工具栏：`新建项目` `从任务导入` 搜索框 筛选(状态/模式/人物)
- 卡片/表格列：封面(可选首帧)· 标题 · 人物 · 模式 · 语言 · 画幅 · 来源 · 时间 · 大小 · 状态

**详情区**（点一个 project）：
- 播放区：`st.video(mp4)` + 首帧
- 参数快照：模式/主题/人物/语言/画幅/来源/音色/BGM
- 文案：`script.json.script`（可折叠）
- 操作：`重新生成` `复制配置` `导出下载` `打开目录` `重命名` `删除`

**新建 / 导入**：
- `新建项目` → 进入生成向导，完成自动转 project（向导①加"项目名称"字段）。
- `从任务导入` → 在 Tasks 页已有"转为项目"按钮（已完成任务可转）；或 Projects 页直接扫已完成的 tasks 生成 project。

### 3.3 操作实现要点
- 预览：`st.video(utils.task_dir(task_id)/final-1.mp4)`
- 重新生成：复用现有 `_queue_task_restore`/`_apply_restored_params`（已验证）+ 新 `task_id`。
- 删除：`shutil.rmtree` 任务目录 + 移除 projects.json 索引（单用户，物理删除可接受；可选加 30 天回收站）。
- 首帧封面：可选，用 ffmpeg 抽第 1 秒帧（`app/utils` 已有 ffmpeg 工具）；失败则用占位图。

---

## 4. 分期路线图

| 阶段 | 内容 | 产出 |
|------|------|------|
| **P0**（本次） | ① 模块拆分（webui/ 6 模块） ② 统一向导主线 ③ Projects 页面+数据模型+新建/列表/详情/重生成/导出/删除 | 可用项目；73 测试全绿；Playwright 冒烟 |
| P1 | 发布闭环（⑤ 预览→多平台一键发布，整合 `upload_post`）；实时进度+日志流；首帧封面 | 生成→发布闭环 |
| P2 | 模板库；Projects 多 task 分组；响应式/移动 | 产品化 |

---

## 5. 风险与边界
- 拆模块会改变 import/符号名 → 用 `webui/__init__.py` 做 facade，`Main.py` 逻辑原样搬、不重写，降低回归风险。
- Projects 指向 task 目录 → 若用户直接删 task 目录，project 变"失效引用"，列表需容错（灰色+提示）。
- `st.video` 播放大 mp4 → 流式播放即可，不缓存。
- 不碰 `app/` 服务层与 API 契约；Projects 纯前端+本地文件层。

---

## 6. 需要你确认的 3 件事

1. **Projects 数据模型**：采用"轻量索引指向 task 目录"（不复制重文件）？还是你想要 project 自带一份完整参数快照+独立封面（更独立但更重）？
2. **模块拆分粒度**：按上面 6 个模块（main/generate/panels/steps/restore/topbar/projects）分，还是你希望更细/更粗（比如 panels 再拆 script.py/video.py/audio.py/subtitle.py）？
3. **P0 收尾标准**：
   - Projects 必须含：列表、详情播放、重生成、导出、删除（我默认这 5 项）
   - 首帧封面：**P0 做**（需 ffmpeg 抽帧）还是 **P1**（占位图即可）？
   - 完成后我用 Playwright 验证哪几条路径（我默认：新建→完成→出现在列表→播放→重生成）？

**确认这 3 点，我即可开始 P0 落地（先模块拆分+结构，再 Projects）。**
