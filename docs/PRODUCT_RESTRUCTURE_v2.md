# MoneyPrinterTurbo 产品诊断与重构方案 v2

> 状态:**P0/P1/P2 已全部落地并真实验证**(2026-10-09)。
> 定位:单用户本地 AI 视频工作室 —— 生成 → 质量 → 管理 闭环。
> 原则:引擎不重写(已验证可用),修复"开箱即失败",补质量闸门,做实作品管理。

---

## 完成清单(全部经真实运行验证)

### P0 开箱即用 ✅
- `app/services/task.py::_normalize_voice_params`:空音色回填 `config.ui`→`config.app` 默认。
- `_voice_preflight_error`:空音色 / 付费 TTS(elevenlabs/fish_audio/minimax)缺 Key
  → 秒级 `preflight` 失败,错误可操作。
- `config.toml` + `config.example.toml` 的 `[ui]` 配音三件套修正为可用默认。
- 验证:不传 voice_name 完整生成 → `state=1/progress=100/final-1.mp4`。

### P1 高质量产出 ✅
- `app/services/storyboard.py`(新):普通视频结构化分镜(hook/timeline/closing,
  逐镜 narration + **真实时长起伏** + 逐镜英文 visual_query)。
- 接入 `generate_script`(用户给了自定义 prompt 时尊重之,不自动分镜)与
  `generate_terms`(复用分镜检索词,顺序与解说 1:1)。
- 分镜暂存 `storyboard.json` + 合并进 `script.json`(无进程级全局状态)。
- `app/services/quality_gate.py`(新):ffprobe/ffmpeg 客观校验(时长/流/分辨率/
  黑帧),结论写入 `script.json.quality_report`,失败降级为警告不阻断交付。
- 验证:`script.json` 含 6 分镜、时长 8/7/7/8/7/8s、逐镜检索词、quality passed=true。

### P2 作品管理 ✅
- `webui/projects.py`:数据模型增强(aspect/duration/size/thumbnail/tags,ffmpeg
  抽首帧封面);列表加**模式/状态筛选 + 富卡片(封面/时长/大小)**。
- 修复 `t()` 语言键(原误读 `language`,应为 `ui_language` → 翻译曾失效)。
- `webui/tasks.py`:完成钩子加**生成即入库**(幂等自动登记 project)。
- 新 i18n key ×3(zh/en)+ 注册 `ENGLISH_FALLBACK_KEYS`(i18n 套件绿)。
- 验证:Projects 页渲染 Mode/Status 筛选 + Play/Regenerate/Delete + Import。

### 测试卫生 ✅
- `test/conftest.py`(新):会话级备份/恢复 `config.toml`。此前跑 pytest 会通过
  AppTest 的 `_save_runtime_config()` 把测试音色写回仓库配置,导致随后真实任务
  在音频阶段失败。已验证修复后哈希不变。

---

## 0. 诊断方法(可复现)

全部结论来自**真实运行**,不是读代码推断:
- 通过项目自己的 API 提交任务,轮询状态机(`state/progress/failed_stage/error`)。
- 通过项目自己的 `app.services` 客户端调用 LLM / TTS(不是裸 curl)。
- `ffprobe` 探查产物;`ast` 审计装饰器/组件/符号。

---

## 1. 事实:哪些能用,哪些不能用

### 1.1 引擎是好的(必须保留,不重写)
显式传参的一次端到端**成功**:`state=1 progress=100`,产出
`final-1.mp4` = **1080x1920 / 41.9s / H.264+AAC / 7.1Mbps / 37MB**,含字幕
`subtitle.srt` 与合规 `final-1.compliance.json`。印证:脚本→文案→音频→字幕→
素材→合成 六阶段可用。

### 1.2 「根本无法使用」的真实根因(已复现,3 条)

**R1 —— 开箱配置自相矛盾,音频阶段必失败。**
`config.toml` 存在两套冲突来源:
- `[app]`:`voice_name = "zh-CN-XiaoxiaoNeural"`(正确,但生成不读它)
- `[ui]`:`voice_mode = "none"` + `tts_server = "elevenlabs"` + `voice_name = ""`(生成实际读这个)

证据:`tts_provider.synthesize(voice_name="")` → `azure_tts_v1 - failed,
Invalid voice ''` ×3 → 返回 `None` → `generate_audio` 报 "failed to synthesize
audio"。三个真实任务(2d416506 / 09c56ae9 / 87e62e10)**全部 error 停在
`failed_stage=audio`**。有音色时同一调用 1.48s 成功。

**R2 —— `VideoParams.voice_name` 默认空字符串,API 调用必踩。**
`TaskVideoRequest(VideoParams)` 直接继承默认值 `voice_name=''`。任何不显式
传音色的请求(API/CLI/历史任务还原)都会在音频阶段失败。无 fail-fast,用户
等数十秒只得到一句 "verify the selected voice and TTS connectivity"。

**R3 —— 无失败快速校验,时间/额度被浪费。**
`start()` 的 preflight 只校验各素材源的 API Key 与 ffmpeg,**不校验配音三件套
的相容性**(voice_mode=tts 时 voice_name 不能为空、tts_server 不能是缺 Key 的
付费源)。配置矛盾要跑到音频阶段才暴露。

### 1.3 其它已确认缺陷
- **D1 任务卡顿无提示**:脚本阶段实测 >60s,进度条长期停在 `progress=5`,
  无阶段名、无日志流给网页端(日志仅在 WebUI 路径采集,API 路径 `add_task`
  直接传 `tm.start`,绕过 `_run_generation`,既不采集日志也无一致性锁)。
- **D2 `local` 素材源失效**:`storage/local_videos/` 有 9 个素材,但
  `video_source=local` 报 "no valid local video materials were found"。
- **D3 Projects 数据模型过薄**:`projects.json` 仅有
  `id/task_id/title/mode/person/language/source/created_at/updated_at`;
  缺 thumbnail/status/aspect/size/tags,列表无状态、无封面、无筛选,不是
  "作品管理"。
- **D4 质量弱**:普通视频走**单段脚本 + 通用库存素材**,无"分镜/画面-解说
  对齐"(传记模式有 shot 结构,普通模式没有);默认 `bgm_type=random`、
  `video_clip_duration=5`;无成片前质量闸门(时长/字幕/黑帧/音频是否存在)。

---

## 2. 目标与验收

| 目标 | 验收标准(可测) |
|------|----------------|
| **开箱可用** | 全新配置下 WebUI 点"生成视频"即出成片;无音色/缺 Key 时**提交即拦截**,提示可操作 |
| **高质量产出** | 普通视频也走分镜结构;成片前质量闸门(时长>0/有音轨/有字幕/无全黑帧);默认参数产出可用成片 |
| **作品管理** | Projects 具备:封面、状态、画幅/来源/大小/时长、搜索/筛选、重命名、重生成、导出、删除、失效引用容错 |

---

## 3. 重构分期

### P0 —— 让它能用(最高优先,直接解决"无法使用")
1. **配置一致性修复**:`config.toml` 的 `[ui]` 配音三件套对齐为可用默认
   (`voice_mode="tts"`, `tts_server="azure-tts-v1"`,
   `voice_name="zh-CN-XiaoxiaoNeural"`, rate=1.0)。`config.example.toml` 同步。
2. **preflight 相容性校验**(`app/services/task.py`):`voice_mode==tts` 时
   校验 voice_name 非空且 tts_server 可用;`upload` 时校验有音频;
   `none` 时跳过。失败即 `_mark_task_failed(stage="preflight")`,秒级返回。
3. **WebUI 提交前同样校验**(`webui/generation.py`):与后端同源规则,按钮
   旁提示,避免提交后才失败。
4. **API 默认兜底**:`voice_name` 为空时回退 `config.ui["voice_name"]` /
    `[app]` 的可用默认,或直接 fail-fast(二选一,取 fail-fast + 明确提示)。

### P1 —— 产出高质量视频
5. **普通视频分镜化**:复用传记的 shot 结构(`shot_audio`/`shot_timings`/
   `visual_type`),让脚本按镜、素材按镜、字幕按镜对齐。
6. **成片质量闸门**(新增 `app/services/quality_gate.py`):合成后 ffprobe
   校验 时长/视频流/音频流/分辨率 与 ffmpeg 全黑帧抽样;不达标标记为
   质量警告并进入 `script.json`,不静默交付。
7. **默认参数优化**:`video_clip_duration`、`bgm_type`、`paragraph_number`
   给出产出可用成片的默认;素材按解说时长精确取用(已有 stop-at-duration)。

### P2 —— 作品管理
8. **Projects 数据模型增强**:`thumbnail`(ffmpeg 抽首帧)、`status`
   (派生自 state)、`aspect`、`source`、`duration`、`size`、`tags`。
9. **Projects 页面**:封面卡列 + 状态徽标 + 搜索/筛选(模式/状态/人物)+
   重命名 + 重生成 + 导出 + 删除(含回收站式容错:task 目录被删时灰显)。
10. **生成即入库**:向导完成后自动落 Project(带封帧),不再需要手动"从任务导入"。

---

## 4. 边界与风险
- 引擎服务(`task.py` 六阶段、`material.py`、`voice.py`)不重写,只加校验与闸门。
- 每期独立可测、可回退;每期结束跑 `pytest -q` + 真实端到端一次。
- 不碰 git;改动集中 `config*.toml`、`app/services/task.py`、
  `app/services/quality_gate.py`(新)、`webui/generation.py`、
  `webui/projects.py`。

---

## 5. 交付顺序
P0.1 配置 → P0.2 后端校验 → P0.3 前端校验 → P0.4 复验开箱可用
→ P1.5 分镜 → P1.6 闸门 → P1.7 默认 → P2.8 模型 → P2.9 页面 → P2.10 入库