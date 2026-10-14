# 阶段 2 设计文档：统一人物数据模型 + 流水线降级可见化

> 状态：**待确认，未实现**。本文为只读调研产出的设计方案，经确认并收到 "go" 后方进入编码。
> 范围：①以人物为中心的统一数据模型（双文件、软引用、迁移与回滚、目标依赖图、并发、/api/v1 兼容与 /api/v2）；②流水线降级可见化机制（含对异常审计的回应、合规类严重降级的确认闸）。
> 所有现状结论均来自对 `E:\pj-vd\MoneyPrinterTurbo` 的静态取证，附文件:行号。
> 本版已并入用户决策（§二）与补充要求（§三）。

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

- `persons` 由 [`aggregate_persons.py`](scripts/library/aggregate_persons.py#L24) 聚合生成：全名精确匹配、`n<3 且无传记书`则丢弃、facts 只归**首位主体**。别名合并（曹操/孟德/阿瞒）**显式推迟到 app 层**，离线管线不做。
- 连接：`PRAGMA journal_mode=WAL`、`synchronous=NORMAL`、`check_same_thread` 可变（[`common.open_db`](scripts/library/common.py#L127)）。运行期读取层 [`library._conn`](app/services/library.py#L50) 用 `lru_cache(maxsize=2)`、`check_same_thread=False`、`sqlite3.Row`。**语料库缺失/未建表时 `get_conn()` 返回 `None`，调用方降级为空结果**（见 [`library.py:57-70`](app/services/library.py#L57)）——这正是要在 §3 里显式登记的降级点。

**personal.sqlite**（运行期覆盖层，用户数据）——路径 [`person_store.db_path()`](app/services/person_store.py#L63)（env `MPT_PERSONAL_DB`，默认 `storage/library/personal.sqlite`）。表结构 [`_SCHEMA`](app/services/person_store.py#L38)：

| 表 | 主键 | 关键列 |
|---|---|---|
| `groups` | `name` | created_at |
| `group_members` | (group_name, person) | person（姓名 TEXT）, added_at |
| `person_meta` | **`person`（姓名 TEXT）** | dynasty, category, note, hidden, pinned, custom, facts_json, updated_at |

- 语义（[`person_store.py:1-22`](app/services/person_store.py#L1)）：dynasty/category 非空则覆盖 library 值；facts_json 非 NULL 替换聚合要点；hidden=1 从列表移除并清空档案；custom=1 标记用户自建。连接 `timeout=30`、`sqlite3.Row`、每次 `_connect()` 都 `executescript(_SCHEMA)`（幂等建表，无版本管理）。

**person_facts.py**：不拥有库，是 library.sqlite 的**只读消费方**（挖书/章/文建 `FactPack` 供 grounding），懒 import library（[`person_facts.py:275`](app/services/person_facts.py#L275)）。

### 1.2 身份与脆弱点（核心问题）

1. **人物主键是姓名 TEXT**，横跨 `persons.person` / `person_meta.person` / `group_members.person`，无代理 id、无外键。改名/重名/异体字即数据错乱。
2. **别名不统一**：离线仅全名精确匹配，别名合并"推迟到 app 层经 Wikipedia/Wikidata"，但该层归属未定义，成为悬空契约。
3. **两库无强约束关联**：overlay 靠姓名字符串 join，library 重建（`DELETE FROM persons` 再灌）与 personal 覆盖层生命周期不一致。
4. **无迁移机制**：两库各自 `CREATE TABLE IF NOT EXISTS`，无 schema 版本，无法演进字段。
5. **Asset/Script/Video 不在库里**：产物落 `storage/tasks/<id>/` 文件系统 + json sidecar，与人物无结构化关联，"全景人物页"无数据来源；素材无内容哈希、无法去重。

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

**结论**：`library ⇄ person_store` 是**双向环**，两侧都靠函数内 import 规避；全模块图充斥"延迟 import 破环"，是分层未确立的信号。

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

**前端**：`{name}` 一律是**姓名**做路径参数。写操作封在 [`lib/api/client.ts:155-191`](frontend/src/lib/api/client.ts#L155)；读操作在 [`src/server/queries.ts:88-100`](frontend/src/server/queries.ts#L88)；页面 [`routes/library.tsx`](frontend/src/routes/library.tsx) + `components/library/{docs-panel,persons-panel}`；类型 [`lib/api/types.ts:202-234`](frontend/src/lib/api/types.ts#L202)。11 端点均被前端使用。**尚无 Script/Video/Asset 维度端点。**

---

## 第二部分：统一人物数据模型设计

### 2.1 设计目标（含 §二.1 决策）

1. **以 Person 为聚合根**统一 Person / Fact / Asset / Script / Video。
2. **双文件方案（已定）**：
   - `mpt.sqlite`——**用户数据**（人物、事实、素材索引、脚本快照、成片、分组、覆盖层、别名），**需纳入备份**。
   - `library.sqlite`——**只读、可重建**的语料库（documents/books/chapters/fts/annotations），不进备份、可由离线管线随时重生成。
   - 二者以 `person_id`（或 Wikidata **QID** 等外部稳定标识）做**软引用**，不建跨库外键。
3. **史料库缺失一律降级、绝不报错中断**：`library.sqlite` 不存在/未建表/损坏时，人物档案仍从 `mpt.sqlite` 返回，语料维度返回空并登记一条 `library_unavailable` 降级（见 §3）。
4. 引入**稳定代理主键 `person_id`** + **不透明对外标识 `public_id`** + 别名表，解开"姓名即主键"。
5. **迁移机制**（版本化、dry-run 对拍、可回滚）。
6. **目标依赖图**：单向分层，消除 `library⇄person_store` 双向环。
7. 面向"全景人物页"的 `/api/v2`（不透明 id），并**向后兼容**现有 `/api/v1/library/*`。

### 2.2 目标实体与字段（`mpt.sqlite`）

```sql
PRAGMA user_version;                        -- 单一整型版本，迁移器读写

CREATE TABLE persons (
  person_id    INTEGER PRIMARY KEY,          -- 内部稳定代理键，不外泄
  public_id    TEXT NOT NULL UNIQUE,         -- 不透明对外 id（ULID/随机），/api/v2 用
  qid          TEXT DEFAULT '',              -- Wikidata QID（可空），跨源软锚点
  display_name TEXT NOT NULL,
  slug         TEXT NOT NULL,
  dynasty      TEXT DEFAULT '',
  category     TEXT DEFAULT '',
  note         TEXT DEFAULT '',
  pinned       INTEGER NOT NULL DEFAULT 0,
  origin       TEXT NOT NULL DEFAULT 'library', -- library | custom
  state        TEXT NOT NULL DEFAULT 'active',  -- active | archived（软删除默认态）
  archived_at  TEXT,                          -- 归档时间；NULL=未归档
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
CREATE INDEX idx_person_state ON persons(state, pinned DESC, display_name);

CREATE TABLE person_aliases (               -- 别名/异体/译名，落地"app 层合并"悬空契约
  person_id INTEGER NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
  alias     TEXT NOT NULL,
  source    TEXT DEFAULT '',               -- wiki | wikidata | manual
  PRIMARY KEY (alias, person_id)
);
CREATE INDEX idx_alias_lookup ON person_aliases(alias);

CREATE TABLE facts (                        -- 结构化事实；source/verified/human_confirmed 分字段
  fact_id         INTEGER PRIMARY KEY,
  person_id       INTEGER NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
  ordinal         INTEGER NOT NULL,        -- 时间线顺序
  statement       TEXT NOT NULL,
  source          TEXT NOT NULL DEFAULT 'library', -- library | manual | llm（来源类型）
  source_ref      TEXT DEFAULT '',         -- doc_id / book_id:idx（可定位出处）
  verified        INTEGER NOT NULL DEFAULT 0, -- 经自动/离线核验
  human_confirmed INTEGER NOT NULL DEFAULT 0, -- 人工确认（独立于 verified）
  edited_by_user  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_facts_person ON facts(person_id, ordinal);

CREATE TABLE assets (                       -- 素材/产物索引（文件在磁盘，路径相对存储根）
  asset_id     INTEGER PRIMARY KEY,
  person_id    INTEGER REFERENCES persons(person_id) ON DELETE SET NULL,
  kind         TEXT NOT NULL,             -- material|audio|image|video|thumb
  path         TEXT NOT NULL,             -- 相对 storage 根，如 tasks/<id>/final-1.mp4
  content_hash TEXT NOT NULL,             -- SHA-256，用于去重
  size_bytes   INTEGER NOT NULL DEFAULT 0,
  task_id      TEXT DEFAULT '',
  state        TEXT NOT NULL DEFAULT 'active', -- active | archived（软删）
  meta_json    TEXT DEFAULT '{}',
  created_at   TEXT NOT NULL
);
CREATE INDEX idx_assets_person ON assets(person_id, kind);
CREATE UNIQUE INDEX idx_assets_hash ON assets(content_hash);  -- 内容去重（同哈希复用一行）

CREATE TABLE scripts (                      -- 完整版本快照；成片绑定具体版本
  script_id   INTEGER PRIMARY KEY,
  person_id   INTEGER NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
  task_id     TEXT DEFAULT '',
  kind        TEXT NOT NULL,              -- narration|storyboard|biography_timeline
  version     INTEGER NOT NULL DEFAULT 1, -- 同一 kind 递增版本
  prev_script_id INTEGER REFERENCES scripts(script_id), -- 版本链
  body        TEXT NOT NULL,              -- 完整快照（非 diff）
  report_json TEXT DEFAULT '',            -- 原 script.json.content_report
  created_at  TEXT NOT NULL
);
CREATE INDEX idx_scripts_person ON scripts(person_id, kind, version);

CREATE TABLE videos (                       -- 成片登记，绑定脚本版本
  video_id    INTEGER PRIMARY KEY,
  person_id   INTEGER NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
  script_id   INTEGER REFERENCES scripts(script_id),  -- 绑定生成它的脚本版本 id
  task_id     TEXT NOT NULL,
  path        TEXT NOT NULL,              -- 相对存储根
  content_hash TEXT DEFAULT '',
  duration_s  REAL,
  state       TEXT NOT NULL DEFAULT 'active', -- active | archived
  review_status TEXT NOT NULL DEFAULT 'ok',   -- ok | needs_review（见 §3.4）
  degraded    INTEGER NOT NULL DEFAULT 0,     -- 是否含任意降级
  created_at  TEXT NOT NULL
);

CREATE TABLE groups (name TEXT PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE group_members (
  group_name TEXT NOT NULL REFERENCES groups(name) ON DELETE CASCADE,
  person_id  INTEGER NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
  added_at   TEXT NOT NULL,
  PRIMARY KEY (group_name, person_id)
);

CREATE TABLE library_links (                -- 与只读 library.sqlite 的软引用
  person_id INTEGER NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
  doc_id    TEXT NOT NULL,
  book_id   TEXT NOT NULL DEFAULT '',
  role      TEXT NOT NULL DEFAULT 'source', -- source|primary_bio
  PRIMARY KEY (person_id, doc_id)
);

CREATE TABLE review_confirmations (         -- 合规类严重降级的"我已知晓"留痕（见 §3.4）
  confirm_id  INTEGER PRIMARY KEY,
  video_id    INTEGER NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
  codes_json  TEXT NOT NULL,               -- 被确认的降级 code 列表
  confirmed_at TEXT NOT NULL
);
```

**要点**
- `person_id`（内部）与 `public_id`（不透明对外）分离：URL 与前端只见 `public_id`/QID，绝不暴露自增整型；`qid` 作跨源软锚点。
- **素材路径一律相对存储根**（`storage/`）记录，配合 `content_hash`（SHA-256）去重；移动存储根不破坏引用。
- **软删除默认**：`persons/assets/videos.state='archived'` + `archived_at`；硬删除（真 `DELETE` + 清文件）仅在二次确认后执行（见 §2.7）。
- `library.sqlite` 仍是 documents/books/chapters/fts 只读全文源；`mpt.sqlite` 通过 `library_links` 以 doc_id/book_id 软引用，不复制正文。
- **脚本存完整版本快照**（`version`+`prev_script_id` 版本链），**成片绑定具体 `script_id`**，可追溯"这条视频用了哪一稿"。
- **事实三字段分离**：`source`（来源类型）/ `verified`（自动核验）/ `human_confirmed`（人工确认）互不等价。

### 2.3 迁移、对拍与回滚（含 §三 dry-run 要求）

**迁移器**：新增 `app/services/db_migrations.py`，`PRAGMA user_version` 版本化，迁移列表 `[(version, up, down)]`。

- **M0（v0→v1）建表**：上述 `mpt.sqlite` schema。
- **M1（v1→v2）数据搬运**（一次性、幂等、默认 dry-run）：
  1. `library.persons` → 建 `persons(display_name, dynasty, origin='library')` + `person_aliases(person)`（生成 `public_id`）；`top_docs/books` → `library_links`；`facts_json` → `facts(source='library', verified=1)`。
  2. `personal.person_meta` → 姓名匹配 `person_id`，合并覆盖字段；`custom=1`→`origin='custom'`；`hidden=1`→`state='archived'`。
  3. `personal.groups/group_members` → 姓名映射 `person_id` 后写入。
  4. 扫 `storage/tasks/*` 既有产物 → 依 task 记录人物名回填 `videos/scripts/assets`（计算 `content_hash`、路径相对化），匹配不到留空/`person_id NULL` 并入"无法映射清单"。

- **dry-run 对拍报告（强制先跑）**：`--dry-run` 不落库，输出：
  - 各表**迁移前后条数对比**（persons/facts/assets/scripts/videos/groups/aliases…）；
  - **无法映射记录清单**（姓名匹配不到 person_id、sidecar 缺字段、文件缺失等），逐条附原因；
  - 哈希冲突/去重命中数。人工核对无误再 `--apply`。

- **旧库不可变（§三）**：迁移成功后 `library.sqlite`、`personal.sqlite` **保持原样，绝不修改或删除**；`mpt.sqlite` 为独立新文件。回滚即停用 `mpt.sqlite`、读写路径切回双库（`storage_source` 开关）。

- **测试夹具（§三）**：用真实 `library.sqlite`/`personal.sqlite`/`storage/tasks` 的**脱敏副本**构造迁移测试夹具，跑 M1 后断言条数对拍、别名解析、软引用完整。

### 2.4 目标依赖图（解开循环）

新增单一数据访问层 **`app/services/person_repo.py`**（唯一持有 `mpt.sqlite` 读写），确立单向分层：

```
controllers (v1 兼容 / v2)
      │  仅依赖 service 门面
      ▼
biography / shot_audio / video / task   （编排层）
      │
      ├──► person_repo   ──► mpt.sqlite   （聚合根 + overlay + facts + 产物索引）
      │        ▲  （只读引用 doc_id/book_id；library 缺失→空 + 降级）
      ├──► library（纯只读语料查询/FTS，不再 import 上层）
      │
      └──► archives/*（依赖 library + person_repo，不再互相回指）
```

- **拆双向环**：`apply_overlay` 从 `library.py` 上移到 `person_repo`；`library.py` 变纯只读语料层，不 import 上层；`person_store.py` 职责并入 `person_repo`，退役反向 import。
- 依赖一律向下；再出现环即视为分层错误，评审拦截。

### 2.5 /api/v1 兼容 与 /api/v2（含 §二.2 决策）

- **新接口一律用不透明 `person_id`（`public_id`）**：`/api/v2/*` 与人物详情页路径/查询只用不透明 id，不出现姓名、不自增整型外露。
- **旧 `/api/v1/library/*` 冻结并标记 deprecated**：`{name}` 继续接受姓名/别名，后端经 `person_aliases` 解析到 `public_id` 再走新库；响应体保持现字段（可增量加 `public_id`），不改语义。OpenAPI 打 `deprecated: true`，文档指引迁移 `/api/v2`。
- 全景人物端点（v2）：`GET /api/v2/persons/{public_id}/overview` 及 `facts|assets|scripts|videos` 分项。
- 旧前端不改也能跑；新前端走 v2 不透明 id。

### 2.6 交付顺序（收到 "go" 后执行，逐步小步提交）

1. `person_repo` + 迁移器骨架 + M0 建表（纯新增，不改现有行为）。
2. M1 + dry-run 对拍报告 + 脱敏夹具测试（条数/别名/软引用一致）。
3. controller 切 person_repo（v1 保响应契约 + 别名解析；引入 v2 不透明 id）。
4. 摘除 `library⇄person_store` 反向 import，收敛依赖图。
5. v2 overview 端点 + 前端全景页。
6. `storage_source` 灰度→默认单库→（独立提交）移除旧读写路径。

### 2.7 SQLite 并发与删除语义（§三补充）

- **WAL 启用**：`mpt.sqlite` 开 `PRAGMA journal_mode=WAL`、`synchronous=NORMAL`；`busy_timeout=30000`。
- **写入并发策略：单写者**。所有写经 `person_repo` 串行化——进程内一个写线程/写队列，或依赖 SQLite 写锁 + `busy_timeout` 重试；避免多写者 `SQLITE_BUSY`。读可并发（WAL 允许读写并发）。
- **后台任务访问**：每个后台任务用**独立连接**（`check_same_thread=False` 或每线程一连接），写操作只经 `person_repo` 门面；长任务分批提交，减少写锁持有。
- **删除语义**：默认**软删除**（`state='archived'` + `archived_at`，列表/概览过滤掉）；**硬删除**（真删行 + 删磁盘文件）须显式二次确认，且级联清理 `assets`/`videos`/`review_confirmations`。library.sqlite 语料不属用户数据，不参与删除，仅随离线重建替换。

---

## 第三部分：流水线降级可见化机制

### 3.1 对异常审计的补充回应（先更正 task.py:546 描述）

**核实结果**：[`task.py:544-547`](app/services/task.py#L544) 的 `try` **只包裹 `from app.services import content_check` 一行 import**；检查体 `content_check.check_script(...)`（[`:550`](app/services/task.py#L550) 起）及重试逻辑在 `try` 之外，其内部异常**正常向上传播、并未被此处吞掉**。更正原描述：

- **真实风险**：`content_check` 若在 **import 期** 抛错，`except Exception: return video_script` 让**整段内容质量预检 + 自动重试静默跳过**，既不写 `content_report`（`:604` 达不到）也无日志。
- **用户看到什么**：视频照常产出、界面无异常，但内容闸门实际没跑——短/空/低质脚本不会被自动加长或标注，质量报告与确认屏该维度**空白**，用户无从得知"这一关被跳过"。**属合规类严重降级**（见 §3.4 code `content_check_skipped`）。

### 3.2 机制设计

**统一降级记录**（贯穿流水线结果对象、任务产物、quality_report、合规 sidecar）：

```python
degradation = {
  "stage":        "content_gate",     # script|content_gate|terms|material|tts|shot_audio|storyboard|subtitle|bgm|compose|compliance…
  "code":         "content_check_skipped",   # 机器可读、可 i18n 定位、全平台稳定枚举
  "user_message": "内容质量预检未执行（依赖缺失），成片未做时长/质量把关",
  "severity":     "warn",             # info | warn | blocking
  "compliance":   True,               # 是否属合规类（决定是否进 needs_review 确认闸）
  "context":      {"detail": "ModuleNotFoundError: ..."},  # 截断、脱敏
}
result["degradations"] = [degradation, ...]
```

- **落盘**：`task_artifacts` 在既有 `script.json`/`quality_report`/`*.compliance.json` sidecar 增写 `degradations`；`videos.degraded`（见 2.2）随之置位；任一 `compliance=True` 的降级把 `videos.review_status` 置 `needs_review`。
- **展示**：结果页与确认屏读取 `degradations[]`，按 severity 呈现（info 折叠、warn 徽标、blocking/合规 顶部横幅 + 确认闸）；`code` 走 i18n 映射文案。

**编码原则**
1. `except Exception` **只允许出现在阶段边界**，且捕获后**必须登记一条 degradation**（禁止裸 `pass`/`continue`/仅 debug）。
2. **阶段内部只捕获具体异常类型**（`sqlite3.Error`、`LookupError`、`OSError`…），能恢复就地恢复、不能恢复上抛交边界统一登记。
3. 降级必须**可观测**：任何跳过/回退都留下 `code`。非合规类不阻断交付，仅提示。

**门禁（CI 可信）**
- 启用 ruff `BLE001`（盲 except）、`S110`（try-except-pass）、`S112`（try-except-continue）。
- **存量**：逐文件基线 + 说明性 `# noqa`（附原因），随重构逐条摘除；**新增代码**严格执行不设豁免。

### 3.3 A 类 8 处按本机制归类（C 类汇入同一登记口）

| 位置 | 现降级 | stage | code | severity | compliance |
|---|---|---|---|---|---|
| [`task.py:544`](app/services/task.py#L544) | import 失败→内容闸门整体跳过 | content_gate | content_check_skipped | blocking | ✅ |
| [`task.py:529`](app/services/task.py#L529) | target_seconds→0（时长判定失效） | content_gate | target_seconds_unavailable | info | ✗ |
| [`biography.py:112`](app/services/biography.py#L112) | grounding={} 放弃史料锚定 | script | grounding_unavailable | warn | ✗ |
| [`historical_archive.py:239`](app/services/archives/historical_archive.py#L239) | `pass`→grounding_block 丢失 | material | grounding_block_lost | warn | ✗ |
| [`shot_audio.py:121`](app/services/shot_audio.py#L121) | 返回全 None→放弃逐镜音频 | shot_audio | per_shot_abandoned | warn | ✗ |
| [`video.py:757`](app/services/video.py#L757) | orientation=1/image_mode=None 静默 | compose | image_probe_failed | info | ✗ |
| [`material.py:1421`](app/services/material.py#L1421) | 下载/校验吞异常→素材缺失 | material | material_fetch_failed | warn | ✗ |
| [`material.py:1429`](app/services/material.py#L1429) | 同上第二分支 | material | material_fetch_failed | warn | ✗ |

**新增（§二.1）**：`library.sqlite` 缺失/未建表 → stage=material（或 grounding 入口）`code=library_unavailable`，severity=warn、compliance=✗，**返回空语料而非报错**，人物档案仍可用。

C 类（warning-only：task.py:1068/598/605、biography 多处、wikimedia 等）统一改为"阶段边界登记 degradation"，用 `severity` 区分，取代散落 `logger.warning` 即丢弃。正例范式参照 [`video.py:1853`](app/services/video.py#L1853)（BGM 混音失败显式置 `bgm_mix_succeeded=False`）。

### 3.4 合规类严重降级确认闸（§二.3 决策）

**仅合规类**降级触发拦截，其余只提示不阻断。合规类降级码清单（`compliance=True`）：

| code | 含义 | 触发 |
|---|---|---|
| `ai_label_burn_in_failed` | AI 标识/水印烧录失败 | 成片未嵌入规定的 AI 生成标识 |
| `license_info_missing` | 授权信息缺失 | 素材授权/许可字段不完整（对齐 material 授权过滤与合规 sidecar） |
| `quality_gate_not_passed` | 质量闸门未通过 | quality_gate 判定不达标 |
| `content_check_skipped` | 内容预检被跳过 | §3.1：content_check import 期失败致闸门空跑 |

**行为**：
1. 命中任一合规降级 → 成片进入 `videos.review_status='needs_review'`（不阻断生成，但阻断"静默导出"）。
2. 结果页/确认屏必须展示这些合规项，且**用户显式点击"我已知晓"后方可导出**。
3. 确认写入 `review_confirmations`（video_id + 被确认的 `codes_json` + 时间）并回写合规 sidecar，形成可追溯记录。
4. 未确认时导出按钮禁用并说明原因；`severity=blocking` 非合规项按 warn 展示不强制确认。

---

## 附：已定决策 & 仍开放

- 双文件（`mpt.sqlite` 用户数据需备份 + `library.sqlite` 只读可重建），软引用 `person_id`/QID。
- `/api/v2` 与详情页用不透明 `person_id`；`/api/v1/library/*` 冻结接受姓名、标 deprecated。
- 合规类严重降级 → `needs_review` + "我已知晓" 方可导出 + sidecar 留痕；其余仅提示。
- 素材相对存储根路径 + SHA-256 去重；软删除默认、硬删除二次确认；WAL + 单写者；脚本版本快照、成片绑脚本版本；事实三字段；迁移 dry-run 对拍 + 旧库不动 + 回滚 + 脱敏夹具。
- **仍开放（待你审阅）**：`public_id` 生成算法（ULID vs 随机 hex）；QID 为空时的软引用仅靠 `doc_id/book_id`；`/api/v2` 是否与 v1 同进程共存还是分控制器挂载。
