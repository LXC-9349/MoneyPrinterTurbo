# MoneyPrinterTurbo 生产化路线(数据管理 + 内容质量)

> 定位:单用户本地 AI 视频工作室。目标:达到「成熟产品」水准,聚焦**数据管理**与**内容质量**。
> 本文件是执行记录 + 路线,所有结论来自真实运行/真实数据。

---

## 0. 真实审计(执行前,数据说话)

| 指标 | 审计值 | 结论 |
|------|--------|------|
| 任务目录 | 68 | 磁盘上有大量产物 |
| 有 script.json | 24 | |
| 有成品 final-*.mp4 | 12 | |
| **projects 入库条目** | **8** | ❌ 12 个成片只入库 8,有遗漏 |
| projects 字段覆盖 | 封面 2/8、时长 2/8、大小 2/8、tags 2/8 | ❌ 卡片大片空字段 |
| 封面文件 | **1** | ❌ 列表几乎全空图 |
| 有分镜 storyboard | 3/24 | 分镜只在新任务生效(旧任务早于模块) |
| 有质量报告 | 4/24 | 同上 |

**根因**:作品库依赖用户**手动**点"从任务导入",且入库时若成片尚未生成/字段拿不到就永久留空 —— 没有自愈机制。

---

## 1. 数据管理(生产级要求)

### 已落地 ✅
- **加载即自愈 `_sync_projects()`**(webui/projects.py):项目页每次渲染前
  1. `_import_all_completed_tasks()` —— 把所有有成品、尚未入库的任务幂等补登;
  2. `_backfill_snapshot_fields()` —— 为历史条目补齐封面/时长/大小/画幅/标签(不覆盖用户已编辑的 title,不臆造缺失数据)。
- **实测效果**:projects 条目 **8 → 13**;字段覆盖 **→ 100%(13/13)**;封面 **1 → 12**。
- 封面抽取 `_ensure_thumbnail()`(ffmpeg 首帧,已存在则复用)。

### 后续(P1)
- [x] 作品**重命名**(projects_hint 承诺,已实现:详情页可编辑标题,写索引不动任务目录)
- [x] 排序:最新/标题/时长/体积(列表筛选行新增排序选择器)
- [x] 批量删除(multiselect 选择 + 一键删除,含任务目录)
- [x] 标签编辑(详情页 expander 内逗号分隔,写索引不动任务目录)
- [x] 早期任务的**客观报告回填 `_backfill_task_reports()`**(projects.py):
  质量闸门/内容预检上线前产出的成片,`script.json` 没有 `quality_report`/`content_report`,
  卡片徽章恒为空。项目页自愈时对该类任务用**管线同款判定器**重算并写回——
  质量走 `quality_gate.check_video_quality`(ffprobe 客观校验,零 LLM,列表 + video_path),
  内容走 `content_check.check_script`(目标时长复用线上 `_content_target_seconds`)。
  幂等且**只补不写**:仅在某字段缺失时写,已有报告绝不覆盖。实测 12 成片补齐
  8 份质量 + 12 份内容报告;两枚 `too_short` 真实落在短片(`1d48c3a7`/`d49f419a`),
  徽章即时渲染 `Content ⚠ 1`。

---

## 2. 内容质量(生产级要求)

### 已具备
- `app/services/storyboard.py`:结构化分镜(hook/timeline/closing,逐镜 narration +
  duration_hint + visual_query),画面-解说按镜对齐、时长有真实起伏。
- `app/services/quality_gate.py`:成片客观校验(时长/视频流/音频流/分辨率/黑帧),
  结论写回 `script.json.quality_report`,**失败降级为警告不阻断交付**。

### 本轮新增 ✅
- **质量可见化**:作品卡片直接显示质量徽章(质量 ✓ / 质量 ⚠ N 项问题),
  从 `script.json.quality_report` 派生。质量不再只躺在日志里。
- **内容质量预检 `app/services/content_check.py`**(新):确定性检查(空脚本/
  估算时长与目标偏差/句子重复/模板占位痕迹),接入 `generate_script`,结论写入
  `script.json.content_report`(**不阻断交付**,只留痕)。作品卡片新增内容徽章
  (内容 ✓ / 内容 ⚠ N)。中英文播报时长按 5 字/秒、2.6 词/秒折算,不调用大模型。

### 后续(P2)
- [ ] 质量未通过的成片,卡片上给出**一键重生成**入口(已有 Regenerate,可加提示)
- [ ] 内容预检不达标时的**自动重试**(生成更长的脚本,而非仅提示)
- [ ] 质量/内容阈值可配置(最短时长、分辨率下限)

---

## 3. 验证纪律

- 每个改动都用**真实运行**验证(实际打开页面读 DOM / 实际跑 pipeline),不靠读代码推断。
- 全量 pytest 对齐**基线 22 failed / 1277 passed**,零新增回归。
- 配置/base 层改动后必须 `fuser -k 8501/tcp` 硬重启(WSL poll watching 不保证热加载)。