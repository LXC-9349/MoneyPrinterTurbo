# 阶段 2 设计文档：统一人物数据模型 + 流水线降级可见化

> 状态：**待确认，未实现**。本文为只读调研产出的设计方案，经确认后方进入编码。
> 范围：①以人物为中心的统一数据模型（合并双库、迁移与回滚、目标依赖图、/api/v1 兼容）；②流水线降级可见化机制（含对异常审计的回应）。
> 所有现状结论均来自对 `E:\pj-vd\MoneyPrinterTurbo` 的静态取证，附文件:行号。

---

## 第一部分：现状盘点（事实）

### 1.1 两个 SQLite 库

**library.sqlite**（离线可重建，运行期只读）——由 `scripts/library/*.py` 构建，路径解析在 [`library.db_path()`](app/services/library.py#L41)（env `MPT_LIBRARY_DB`，默认 `storage/library/library.sqlite`；文件不存在返回 `None`）。表结构见 [`scripts/library/common.py:_SCHEMA`](scripts/library/common.py#L78)：

| 表 | 主键 | 关键列 |
|---|---|---|
| `documents` | `doc_id`（源路径 sha1[:16]，前缀 `d-`） | source, category, title, path, body, n_chars, extracted_at |
| `books` | `book_id`（`b-`+sha1[:16]） | source, title, author, path, format, status, n_chapters, n_chars, error, processed_at |
| `chapters` | (book_id, idx) | book_id→books, title, body |
| `docs_fts` / `books_fts` | FTS5 **contentless**(`content=''`) | title/body(/author)，需从内容表自算 snippet |
| `annotations` | (target_type, target_id) | json（LLM 标注：persons[], dynasty, reliable, key_facts, is_biography, subject…）, model |
| `persons` | **`person`（姓名 TEXT）** | dynasty, doc_count, reliable_high, facts_json, top_docs(json), books(json) |

- `persons` 由 [`aggregate_persons.py`](scripts/library/aggregate_persons.py#L24) 聚合生成：全名精确匹配、`n<3 且无传记书`则丢弃、facts 只归**首位主体**（避免李世民继承武则天的条目）。别名合并（曹操/孟德/阿瞒）**显式推迟到 app 层**，离线管线不做。
- 连接：`PRAGMA journal_mode=WAL`、`synchronous=NORMAL`、`check_same_thread` 可变（[`common.open_db`](scripts/library/common.py#L127)）。运行期读取层 [`library._conn`](app/services/library.py#L50) 用 `lru_cache(maxsize=2)`、`check_same_thread=False`、`sqlite3.Row`。

**personal.sqlite**（运行期覆盖层，用户数据）——路径 [`person_store.db_path()`](app/services/person_store.py#L63)（env `MPT_PERSONAL_DB`，默认 `storage/library/personal.sqlite`）。表结构 [`_SCHEMA`](app/services/person_store.py#L38)：

| 表 | 主键 | 关键列 |
|---|---|---|
| `groups` | `name` | created_at |
| `group_members` | (group_name, person) | person（姓名 TEXT）, added_at |
| `person_meta` | **`person`（姓名 TEXT）** | dynasty, category, note, hidden, pinned, custom, facts_json, updated_at |

- 语义（见模块 docstring [`person_store.py:1-22`](app/services/person_store.py#L1)）：dynasty/category 非空则**覆盖** library 值；facts_json 非 NULL **替换**聚合要点；hidden=1 从列表移除并清空档案；custom=1 标记用户自建、库中不存在的人物。连接 `timeout=30`、`sqlite3.Row`、每次 `_connect()` 都 `executescript(_SCHEMA)`（幂等建表，无版本管理）。

**person_facts.py**：不拥有库，是 library.sqlite 的**只读消费方**（挖书/章/文建 `FactPack` 供 grounding），懒 import library（[`person_facts.py:275`](app/services/person_facts.py#L275)）。

### 1.2 身份与脆弱点（核心问题）

1. **人物主键是姓名 TEXT**，横跨 `persons.person` / `person_meta.person` / `group_members.person` 三处，无代理 id、无外键。改名/重名/异体字即数据错乱。
2. **别名不统一**：离线聚合仅全名精确匹配，别名合并"推迟到 app 层经 Wikipedia/Wikidata"——但该 app 层归属未定义，成为悬空契约。
3. **两库无强约束关联**：overlay 靠姓名字符串 join，library 重建（`DELETE FROM persons` 再灌）与 personal 覆盖层生命周期不一致。
4. **无迁移机制**：两库各自 `CREATE TABLE IF NOT EXISTS`，无 `PRAGMA user_version`/schema 版本表，无法演进字段。
5. **Asset/Script/Video 不在库里**：产物落 `storage/tasks/<id>/` 文件系统 + json sidecar，与人物无结构化关联，"全景人物页"无数据来源。

### 1.3 循环依赖（证据）

| 边 | 位置 | import 形态 |
|---|---|---|
| library → person_store | [`library.py:337`](app/services/library.py#L337)（apply_overlay） | 函数内延迟 |
| person_store → library | [`person_store.py:225/323/340/477/563`](app/services/person_store.py#L225) | 函数内延迟（5 处） |
| biography → library | [`biography.py:177/202`](app/services/biography.py#L177) | 函数内延迟 |
| biography → person_facts | [`biography.py:127/390`](app/services/biography.py#L127) | 函数内延迟 |
| person_facts → library | [`person_facts.py:275`](app/services/person_facts.py#L275) | 函数内延迟 |
| archives/historical_archive → library | [`historical_archive.py:69/234`](app/services/archives/historical_archive.py#L69) | 函数内延迟 |
| task → biography | [`task.py:346/359/620/1140`](app/services/task.py#L346) | 函数内延迟 |
| controller → library, person_store | [`controllers/v1/biography.py:16`](app/controllers/v1/biography.py#L16) | 顶层（入口层，正常） |

**结论**：`library ⇄ person_store` 是**双向环**，两侧都靠函数内 import 规避；全模块图充斥着"延迟 import 破环"，是典型分层未确立的信号。

### 1.4 /api/v1/library/* 现状（后端 11 端点，全在 `controllers/v1/biography.py`，挂 `/api/v1`）

| 方法 | 路径 | 处理 | 服务层 |
|---|---|---|---|
| GET | `/library/persons` | 列人物（filter） | library + overlay |
| GET | `/library/counts` | 各表计数 | [`:49`](app/controllers/v1/biography.py#L49) |
| GET | `/library/groups` | 自定义分组 | person_store |
| POST | `/library/groups` | 建组 | [`:80`](app/controllers/v1/biography.py#L80) |
| DELETE | `/library/groups/{name}` | 删组 | [`:93`](app/controllers/v1/biography.py#L93) |
| POST | `/library/persons` | 建自建人物 | [`:104`](app/controllers/v1/biography.py#L104) |
| GET | `/library/persons/{name}/documents` | 人物文章列表 | [`:122`](app/controllers/v1/biography.py#L122) |
| GET | `/library/persons/{name}/profile` | overlay 感知档案 | [`:151`](app/controllers/v1/biography.py#L151) |
| PATCH | `/library/persons/{name}` | 改 person_meta | [`:168`](app/controllers/v1/biography.py#L168) |
| PUT | `/library/persons/{name}/groups` | 设分组 | [`:192`](app/controllers/v1/biography.py#L192) |
| POST | `/library/persons/{name}/restore` | 撤销 hidden | [`:210`](app/controllers/v1/biography.py#L210) |

**前端**：`{name}` 一律是**姓名**做路径参数。写操作封在 [`lib/api/client.ts:155-191`](frontend/src/lib/api/client.ts#L155)（createPerson/patchPerson/delete/restore/setGroups/createGroup/deleteGroup/getProfile）；读操作在 [`src/server/queries.ts:88-100`](frontend/src/server/queries.ts#L88)（listPersons/counts/groups/documents）；页面 [`routes/library.tsx`](frontend/src/routes/library.tsx) + `components/library/{docs-panel,persons-panel}`；类型 [`lib/api/types.ts:202-234`](frontend/src/lib/api/types.ts#L202)。11 端点均被前端使用，无僵尸端点。**尚无 Script/Video/Asset 维度端点。**

---

## 第二部分：统一人物数据模型设计

### 2.1 设计目标

1. **以 Person 为聚合根**统一 Person / Fact / Asset / Script / Video。
2. 单一 SQLite 库（`mpt.sqlite`）承载**可变形数据**（人物身份、别名、分组、覆盖、事实、产物索引）；离线语料库 `library.sqlite` 保持**只读源**定位不变。
3. 引入**稳定代理主键 `person_id`** + 别名表，解开"姓名即主键"的脆弱耦合。
4. **迁移机制**（版本化、可前滚/回滚）。
5. **目标依赖图**：单向分层，消除 library⇄person_store 双向环。
6. 面向"全景人物页"的 API，且**向后兼容**现有 11 端点。

### 2.2 目标实体与字段（`mpt.sqlite`）

```sql
-- 版本管理
PRAGMA user_version;                       -- 单一整型版本，迁移器读写

CREATE TABLE persons (
  person_id   INTEGER PRIMARY KEY,         -- 稳定代理键，跨重命名/别名不变
  slug        TEXT NOT NULL UNIQUE,        -- 规范化标识（展示名归一）
  display_name TEXT NOT NULL,              -- 主展示名（可改，不动 person_id）
  dynasty     TEXT DEFAULT '',
  category    TEXT DEFAULT '',
  note        TEXT DEFAULT '',
  hidden      INTEGER NOT NULL DEFAULT 0,
  pinned      INTEGER NOT NULL DEFAULT 0,
  origin      TEXT NOT NULL DEFAULT 'library', -- library | custom（取代 custom 标志）
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE person_aliases (              -- 别名/异体/译名，解 Wikipedia 合并悬空契约
  person_id   INTEGER NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
  alias       TEXT NOT NULL,
  source      TEXT DEFAULT '',            -- wiki | wikidata | manual
  PRIMARY KEY (alias, person_id)
);
CREATE INDEX idx_alias_lookup ON person_aliases(alias);  -- 别名 → person_id 反查

CREATE TABLE facts (                       -- 结构化事实，可溯源，取代 facts_json blob
  fact_id     INTEGER PRIMARY KEY,
  person_id   INTEGER NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
  ordinal     INTEGER NOT NULL,           -- 时间线顺序
  statement   TEXT NOT NULL,
  source_ref  TEXT DEFAULT '',            -- doc_id / book_id:idx
  verified    INTEGER NOT NULL DEFAULT 0, -- 来自 library 聚合 or 人工
  edited_by_user INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_facts_person ON facts(person_id, ordinal);

CREATE TABLE assets (                      -- 素材/产物落库索引（文件仍在磁盘）
  asset_id    INTEGER PRIMARY KEY,
  person_id   INTEGER REFERENCES persons(person_id) ON DELETE SET NULL,
  kind        TEXT NOT NULL,              -- material|audio|image|video|thumb
  path        TEXT NOT NULL,
  task_id     TEXT DEFAULT '',
  meta_json   TEXT DEFAULT '{}'
);
CREATE INDEX idx_assets_person ON assets(person_id, kind);

CREATE TABLE scripts (                     -- 生成/用户脚本 + content_report
  script_id   INTEGER PRIMARY KEY,
  person_id   INTEGER NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
  task_id     TEXT DEFAULT '',
  kind        TEXT NOT NULL,              -- narration|storyboard|biography_timeline
  body        TEXT NOT NULL,
  report_json TEXT DEFAULT '',            -- 原 script.json.content_report
  created_at  TEXT NOT NULL
);

CREATE TABLE videos (                      -- 成片登记
  video_id    INTEGER PRIMARY KEY,
  person_id   INTEGER NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
  task_id     TEXT NOT NULL,
  path        TEXT NOT NULL,
  duration_s  REAL,
  degraded    INTEGER NOT NULL DEFAULT 0, -- 见第三部分：是否含降级
  created_at  TEXT NOT NULL
);

CREATE TABLE groups (name TEXT PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE group_members (
  group_name TEXT NOT NULL REFERENCES groups(name) ON DELETE CASCADE,
  person_id  INTEGER NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
  added_at   TEXT NOT NULL,
  PRIMARY KEY (group_name, person_id)
);

CREATE TABLE library_links (               -- 与只读 library.sqlite 的软引用
  person_id  INTEGER NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
  doc_id     TEXT NOT NULL,               -- 指向 library.documents
  book_id    TEXT NOT NULL DEFAULT '',
  role       TEXT NOT NULL DEFAULT 'source', -- source|primary_bio
  PRIMARY KEY (person_id, doc_id)
);
```

**要点**
- `person_id` 为不变代理键；`display_name`/`slug` 可改而不断裂引用；`person_aliases` 承接原"推迟到 app 层"的别名合并，成为可落库的一等数据。
- 覆盖层语义整体并入 `persons`（dynasty/category/note/hidden/pinned/origin）与 `facts`（`edited_by_user` 区分聚合事实 vs 用户编辑，取代"facts_json 非 NULL 整体替换"）。
- `library.sqlite` 仍是 documents/books/chapters/fts 的**只读全文源**；`mpt.sqlite` 通过 `library_links` 以 doc_id/book_id 软引用，不复制正文（避免双写、保留离线可重建）。
- Asset/Script/Video 只存**索引 + sidecar 引用**，大文件仍在 `storage/tasks/`，DB 保证"全景人物页"可枚举。

### 2.3 迁移与回滚

**迁移器**：新增 `app/services/db_migrations.py`，用 `PRAGMA user_version` 版本化，迁移列表 `[(version, up_sql_or_fn, down_sql_or_fn)]`，启动/CLI 前滚：

- **M0（v0→v1）建表**：如上 `mpt.sqlite` schema。
- **M1（v1→v2）数据搬运**（一次性、幂等）：
  1. 读 `library.persons` → 每行建 `persons(display_name=person, dynasty, origin='library')` + `person_aliases(person)`；`top_docs`/`books` → `library_links`；`facts_json` → `facts(verified=1)`。
  2. 读 `personal.person_meta` → 按 person 名匹配 `person_id`，合并覆盖字段；`custom=1` → `origin='custom'`。
  3. 读 `personal.groups/group_members` → 姓名映射到 `person_id` 后写入。
  4. 扫描 `storage/tasks/*` 既有产物 → 依据 task 记录的人物名回填 `videos/scripts/assets`（能匹配则匹配，匹配不到留空、`person_id NULL`）。
- **回滚**：迁移前对 `storage/library/*.sqlite` 做带时间戳冷拷贝（复用现有 bundle 纪律，非云同步目录）；每个 M 提供 `down`，失败即事务回滚并保留冷拷贝。M1 为**只增不改**（新库独立文件），回滚=删除 `mpt.sqlite` 回到双库读写路径，风险可控。

**兼容窗口（关键）**：引入 `storage_source` 开关（config），迁移后仍可切回双库读，灰度验证一致后再摘除旧路径。

### 2.4 目标依赖图（解开循环）

新增单一数据访问层 **`app/services/person_repo.py`**（唯一持有 `mpt.sqlite` 读写），确立单向分层：

```
controllers (v1)
      │  仅依赖 service 门面
      ▼
biography / shot_audio / video / task   （编排层）
      │
      ├──► person_repo   ──► mpt.sqlite   （人物聚合根 + overlay + facts + 产物索引）
      │        ▲
      │        │（只读引用 doc_id/book_id）
      ├──► library（只读语料查询/FTS，删除对 person_store 的依赖）
      │
      └──► archives/*（依赖 library + person_repo，不再互相回指）
```

- **拆除双向环**：`apply_overlay` 从 `library.py` 上移到 `person_repo`（由"读 library 再叠 personal"改为"person_repo 组合 library 只读结果"）。`library.py` 变成**纯只读语料层，不 import 任何上层**；`person_store.py` 的职责并入 `person_repo`，退役对 library 的反向 import。
- 依赖方向一律向下（编排层 → 数据层），杜绝函数内延迟 import 破环的权宜；若仍出现环，视为分层错误信号在评审阶段拦截。

### 2.5 /api/v1 兼容策略

- **现有 11 端点契约不变**：`{name}` 路径参数继续接受姓名/别名，controller 内经 `person_aliases` 解析为 `person_id` 再走新库；响应体保持现字段，**新增**可选 `person_id` 字段（增量、非破坏）。
- **新增全景人物端点**（只增）：`GET /library/persons/{id}/facts|assets|scripts|videos` 与聚合 `GET /library/persons/{id}/overview`，为"全景人物页"提供数据。
- 分页/计数字段沿用现有 `BaseResponse`；旧前端不改也能跑，新前端逐步切 `person_id`。

### 2.6 交付顺序（确认后执行，逐步小步提交）

1. `person_repo` + 迁移器骨架 + M0 建表（纯新增，不改现有行为）。
2. M1 数据搬运 + 双库/单库对拍测试（行数、别名、overlay 语义一致性）。
3. controller 切 person_repo（保持响应契约）+ 别名解析。
4. 摘除 library⇄person_store 反向 import，收敛依赖图。
5. 新增 overview 端点 + 前端全景页。
6. `storage_source` 开关灰度→默认单库→移除旧路径（独立删除提交）。

---

## 第三部分：流水线降级可见化机制

### 3.1 对异常审计的补充回应（先更正 task.py:546 描述）

**核实结果**：[`task.py:544-547`](app/services/task.py#L544) 的 `try` **只包裹 `from app.services import content_check` 一行 import**；检查体 `content_check.check_script(...)`（[`:550`](app/services/task.py#L550) 起）及重试逻辑在 `try` 之外，其内部异常**会正常向上传播、并未被此处吞掉**。因此原描述"检查体任意异常被静默吞掉"不准确，更正为：

- **真实风险**：若 `content_check` 因自身可选依赖而在 **import 期** 抛错，`except Exception: return video_script` 会让**整段内容质量预检 + 自动重试静默跳过**，既不写 `content_report`（`:604` 达不到）也无任何日志。
- **用户看到什么**：视频照常产出、界面无异常，但内容质量闸门实际没跑——短/空/低质脚本不会被自动加长或标注，`script.json` 缺 `content_report`，下游质量报告与确认屏对该维度**空白**，用户无从得知"这一关被跳过了"。

### 3.2 机制设计

**统一降级记录**（贯穿流水线结果对象、任务产物、quality_report、合规 sidecar）：

```python
# 每条降级
degradation = {
  "stage":        "content_gate",     # 稳定枚举：script|content_gate|terms|material|tts|shot_audio|storyboard|subtitle|bgm|compose|compliance…
  "code":         "content_check_import_failed",  # 机器可读、可 i18n 定位
  "user_message": "内容质量预检未执行（依赖缺失），成片未做时长/质量把关",
  "severity":     "warn",             # info | warn | error（error 仍交付但醒目提示）
  "context":      {"detail": "ModuleNotFoundError: ..."},  # 截断、脱敏
}
# 结果对象统一携带
result["degradations"] = [degradation, ...]
```

- **落盘**：`task_artifacts` 在既有 `script.json`/`quality_report`/`*.compliance.json` sidecar 增写 `degradations`；`videos.degraded`（见 2.2）随之置位。
- **展示**：前端**结果页**与**确认屏**读取 `degradations[]`，按 severity 呈现（info 折叠、warn 徽标、error 顶部横幅 + 需确认）；`code` 走 i18n 映射文案。

**编码原则**
1. `except Exception` **只允许出现在阶段边界**（编排层调用某阶段的唯一入口处），且捕获后**必须登记一条 degradation**（禁止裸 `pass`/`continue`/仅 debug）。
2. **阶段内部只捕获具体异常类型**（`sqlite3.Error`、`LookupError`、`OSError`…），能恢复就地恢复、不能恢复则上抛交阶段边界统一登记。
3. 降级不阻断交付（除非 severity=error 的硬失败），但必须**可观测**：任何跳过/回退都要留下 `code`。

**门禁（CI 可信）**
- 启用 ruff `BLE001`（盲 except）、`S110`（try-except-pass）、`S112`（try-except-continue）。
- **存量**：逐文件基线——`ruff` per-file-ignores 列出现有允许点 + 说明性 `# noqa: BLE001`（附原因），随重构逐条摘除。
- **新增代码**：严格执行，不设豁免。
- 目标：静默吞异常从"约定"变成"编译期/CI 拦截"。

### 3.3 A 类 8 处按本机制归类（C 类汇入同一登记口）

| 位置 | 现降级 | stage | code | severity |
|---|---|---|---|---|
| [`task.py:544`](app/services/task.py#L544) | import 失败→内容闸门整体跳过 | content_gate | content_check_import_failed | warn |
| [`task.py:529`](app/services/task.py#L529) | `_content_target_seconds`→0（时长判定失效） | content_gate | target_seconds_unavailable | info |
| [`biography.py:112`](app/services/biography.py#L112) | grounding={} 放弃史料锚定 | script | grounding_unavailable | warn |
| [`historical_archive.py:239`](app/services/archives/historical_archive.py#L239) | `pass`→grounding_block 丢失 | material | grounding_block_lost | warn |
| [`shot_audio.py:121`](app/services/shot_audio.py#L121) | 返回全 None→放弃逐镜音频 | shot_audio | per_shot_abandoned | warn |
| [`video.py:757`](app/services/video.py#L757) | orientation=1/image_mode=None 静默 | compose | image_probe_failed | info |
| [`material.py:1421`](app/services/material.py#L1421) | 下载/校验吞异常→素材缺失 | material | material_fetch_failed | warn |
| [`material.py:1429`](app/services/material.py#L1429) | 同上第二分支 | material | material_fetch_failed | warn |

C 类（warning-only：task.py:1068/598/605、biography 多处、wikimedia 等）不各改各的，统一改为"在阶段边界登记 degradation"，用 `severity=info/warn` 区分，取代散落的 `logger.warning` 即丢弃。正例范式参照 [`video.py:1853`](app/services/video.py#L1853)（BGM 混音失败显式置 `bgm_mix_succeeded=False`）。

---

## 附：本设计明确不做 / 待确认点

- 不复制 library 正文进 `mpt.sqlite`（保持离线只读源、软引用）。
- 别名合并的 Wikipedia/Wikidata 数据源实现细节留待子设计（本文只定义 `person_aliases` 落库与解析口）。
- **待你确认**：①单一 `mpt.sqlite` + `library.sqlite` 只读源的双文件方案 vs 彻底合并含正文；②`person_id` 是否对外暴露（建议 overview 端点用 id、旧端点维持姓名）；③降级 `severity=error` 时是否需要"阻断成片需用户显式确认"。
