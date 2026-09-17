# AI 人物传记短视频系统 — 二开交付说明

基于 [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo)(MIT)二次开发。
输入一个人物名 → 输出 3-5 分钟传记解说短视频(结构化旁白 + AI 配音 + 该人物**真实历史照片**
+ 精确字幕 + BGM + AI 生成标识 + 合规 sidecar)。

---

## 一、素材源 `historical_archive`(与原版最大差异)

原版从 Pexels/Pixabay 找泛用素材,传记视频必须用人物本人的历史照片。新增三个免 key 公开源:

| 优先级 | 数据源 | 作用 | 授权过滤 |
|---|---|---|---|
| a | Wikimedia Commons | 人物真实历史照片(主力) | 仅 PD / CC0 / CC BY |
| b | Wikipedia REST | 事实底座 + 封面人像 | — |
| c | Library of Congress | 欧美人物补充照片 | 排除 restricted |

- **授权白名单强制**:CC BY-SA / NC / ND 一律拒绝(成片商用+平台发布,SA 会传染整片授权)
- **噪声过滤**:剔除 PDF/地图等非图片,以及「以人名命名的街道/中学/纪念碑」照片
- **检索增强**:中文名自动解析英文条目(Commons 英文命中率高得多),并用本地史料库的
  朝代英文词拼接检索(如 `Cao Cao Three Kingdoms`)
- **降级链**:某分镜无档案素材 → 换同人物其它照片 → 仍无则整片降级到 Pexels b-roll;
  任何 API 挂掉都不会中断任务

### 配置
```toml
[app]
video_source = "historical_archive"
historical_archive_enabled = true
historical_archive_min_image_width = 640
wikimedia_user_agent = "你的项目名/版本 (联系方式)"   # Wikimedia 要求可识别 UA
material_fallback_source = "pexels"                  # 仅 b_roll / 兜底
```
`.env.example` 已列出全部变量与获取地址;新增变量均读环境变量,无硬编码。

---

## 二、本地史料库(4942 篇文章 + 1256 本书)

`scripts/library/*` 离线流水线,产物在 `storage/library/library.sqlite`(gitignored):

```bash
python scripts/library/ingest_docs.py        # A 库 .doc → 解码入库
python scripts/library/ingest_books.py       # B 库 mobi/epub/azw3 → 章节(断点续跑)
python scripts/library/build_fts.py          # FTS5 索引(CJK 逐字分词)
python scripts/library/annotate.py --target docs   # LLM 标注(可续跑)
python scripts/library/annotate.py --target books
python scripts/library/aggregate_persons.py  # 人物档案聚合
python scripts/library/quality_report.py     # 抽取质量体检
```

实测规模:4942 篇文章 / 1256 本书 / 62999 章节 / 1173 位人物档案 / 索引 914MB。
朝代时间线 `scripts/library/data/dynasty_timeline.json`(十二段,含公元年区间与英文 era 词)。

消费层 `app/services/library.py`(`get_person_profile` / `search_facts` /
`search_book_excerpts` / `grounding_block`),只读、故障即降级。

> **版权红线**:书籍仅作事实来源,接口硬限制单条 snippet ≤160 字,绝不向 prompt
> 输出成段原文;野史类标 `reliable=low` 供生成时排除。

---

## 三、结构化脚本 + 事实校验 `app/services/biography.py`

输出严格 JSON,每个分镜自带视觉类型与归档检索词:

```json
{"hook": "开场钩子",
 "timeline": [{"narration": "…", "duration_hint": 14,
               "visual_type": "historical_photo|generated_scene|b_roll",
               "visual_query": "Albert Einstein patent office 1905"}],
 "closing": "结尾升华"}
```

- 事实底座 = 本地史料库(人物档案 + 书目含年份段落)⊕ Wikipedia 摘要;
  语料 <600 字时自动从已索引书籍补足(实测 260 → 1278 字)
- `verify_shots()` 逐分镜核对年份/地点,未通过标 `verified:false` + 原因;
  语料过薄时降级为「需人工复核」而非误判,不阻断生成
- `visual_type` 驱动素材调度:`historical_photo` 走档案源,`b_roll` 交给库存源

---

## 四、配音与字幕

- `app/services/tts_provider.py`:统一 TTS 入口,`tts_provider` 配置切换;
  `cosyvoice2`(阿里云定制克隆音色)为**预留适配位**,含完整接入步骤,未启用时零影响
- `app/services/shot_audio.py`:**逐分镜合成 + 拼接**,累加时长直接得到精确镜级边界,
  无需强制对齐;字幕与画面对应关系由构造保证
- 实测:第 1 条字幕 `00:00:00,000 → 00:00:14,180` 与分镜音频时长 14.18s 完全一致
- faster-whisper 保持可选(edge-tts 本身即词级时间戳,镜级对齐已由分镜音频覆盖)

---

## 五、合规

- **AI 标识**:烧录式角标(默认「内容由AI生成」,位置/字号/透明度可配),
  在字幕之后叠加,永不被遮挡
- **sidecar** `<视频同名>.compliance.json`:每张图的来源页/授权/作者、
  授权统计、所用 LLM/TTS/字幕模型、`unverified` 镜头清单
- 实测样例:`{Public domain: 8, CC BY 3.0: 1, CC0: 1, CC BY 4.0: 1}`

---

## 六、跑通示例(实测产物)

| 阶段 | task_id | 成片 | 说明 |
|---|---|---|---|
| 1 脚手架 | `3b4c0652` | 54.7s / 1080p | 原版链路,Pexels |
| 2 档案源 | `d49f419a` | 87.1s | 6 张真实历史照片,含授权 manifest |
| 3 结构化脚本 | `40db6227` | 232.5s | 15 分镜,三类 visual_type 生效 |
| 4 分镜音频 | `10589a05` | 209.5s | 15 段分镜音频 + 精确字幕 |
| 5 合规 | `7539d955` | 203.7s | AI 角标 + sidecar(11 源授权齐全) |

产物路径:`storage/tasks/<task_id>/`(`final-1.mp4` / `audio.mp3` / `subtitle.srt` /
`script.json` / `final-1.compliance.json`)。

## 七、启动
```bash
conda activate mpt
python main.py -api                                      # API :8080
streamlit run webui/Main.py --server.port 8501           # WebUI :8501
```
API 调用示例见 `docs/dev-log/phase*.md`;传记任务关键参数:
`video_source="historical_archive"` + `biography_person="爱因斯坦"`。

## 八、已知限制
- 同一人物的档案照片池有限,长视频中少量图片会复用到不同分镜
  (改进方向:同图不同裁切/变速,或按分镜做画面去重)
- 8 篇文章(约 0.2%)因本地网关内容过滤无法生成结构化标注,仍可全文检索
- 外国小众人物在 Commons 覆盖不足时会整片降级为 b-roll
- CosyVoice2 定制音色为接口预留,未实现
