# 阶段 2 设计文档：统一人物数据模型 + 流水线降级可见化

> 状态：**已按 §一 修订，待实现**。§2.6 步骤 1、2 有条件放行（只新增文件、不改现有行为）；步骤 3+ 待 dry-run 报告后另行决定。
> 范围：①以人物为中心的统一数据模型（双文件、软引用、素材拆分、物化与同步、迁移/回滚、并发、一致性、/api/v1 兼容与 /api/v2）；②流水线降级可见化机制（注册表驱动、任务级收集器、合规类需确认）。
> 现状结论均来自对 `E:\pj-vd\MoneyPrinterTurbo` 的静态取证，附文件:行号。
> **文末为本次修订 changelog（逐条对应审阅意见）。**

---

## 第一部分：现状盘点（事实）

### 1.1 两个 SQLite 库

**library.sqlite**（离线可重建，运行期只读，~900MB）——由 `scripts/library/*.py` 构建，路径解析 [`library.db_path()`](app/services/library.py#L41)（env `MPT_LIBRARY_DB`，默认 `storage/library/library.sqlite`；不存在返回 `None`）。表结构 [`scripts/library/common.py:_SCHEMA`](scripts/library/common.py#L78)：

| 表 | 主键 | 关键列 |
|---|---|---|
| `documents` | `doc_id`（`d-`+sha1[:16]） | source, category, title, path, body, n_chars, extracted_at |
| `books` | `book_id`（`b-`+sha1[:16]） | source, title, author, path, format, status, n_chapters, n_chars, error |
| `chapters` | (book_id, idx) | book_id→books, title, body |
| `docs_fts`/`books_fts` | FTS5 **contentless** | title/body(/author)，需自算 snippet |
| `annotations` | (target_type, target_id) | json（persons[], dynasty, reliable, key_facts, is_biography, subject）, model |
| `persons` | **`person`（姓名 TEXT）** | dynasty, doc_count, reliable_high, facts_json, top_docs(json), books(json) |

- `persons` 由 [`aggregate_persons.py`](scripts/library/aggregate_persons.py#L24) 聚合：全名精确匹配、`n<3 且无传记书`丢弃、facts 只归首位主体；别名合并显式推迟到 app 层。
- 连接 [`common.open_db`](scripts/library/common.py#L127)：WAL、`synchronous=NORMAL`；运行期 [`library._conn`](app/services/library.py#L50) `lru_cache(maxsize=2)`、`check_same_thread=False`。**语料库缺失/未建表时 `get_conn()` 返回 `None`**（[`library.py:57-70`](app/services/library.py#L57)），调用方降级为空——这是需显式登记的降级点。

**personal.sqlite**（运行期覆盖层，用户数据）——路径 [`person_store.db_path()`](app/services/person_store.py#L63)（env `MPT_PERSONAL_DB`）。表 [`_SCHEMA`](app/services/person_store.py#L38)：`groups(name)`、`group_members(group_name,person)`、`person_meta(person PK, dynasty, category, note, hidden, pinned, custom, facts_json, updated_at)`。语义（[`person_store.py:1-22`](app/services/person_store.py#L1)）：dynasty/category 非空覆盖 library；facts_json 非 NULL 整体替换聚合；hidden=1 移除并清空档案；custom=1 用户自建。连接 `timeout=30`，每次 `_connect()` `executescript(_SCHEMA)`，**无 foreign_keys、无版本管理**。

**person_facts.py**：library.sqlite 的只读消费方（建 `FactPack` 供 grounding），懒 import（[`person_facts.py:275`](app/services/person_facts.py#L275)）。

### 1.2 projects（工作区，独立于两库）

[`app/services/project_store.py`](app/services/project_store.py#L1)：作品工作区**存于 `storage/projects/projects.json` 单文件**（非 SQLite），原子写盘。条目字段：`id`（`p_`+hex12）、`task_id`（一项目绑一任务）、`title`、`mode`（biography|normal）、`person`（**姓名串**）、`status`（draft|processing|done|failed）、`step`、`draft`（`VideoParams` 全量快照）。**历史遗留条目**（webui 时代）另带 `video_file/duration/size_bytes/thumbnail/subject/tags/aspect` 而**无 `draft/status`**——即 projects.json 是混合 schema。作品列表/重命名/删除/绑定任务均在此层，`{person}` 用姓名，与库无结构关联。

### 1.3 身份与脆弱点

1. 人物主键是**姓名 TEXT**，横跨三处，无代理 id/外键 → 改名/重名/异体字即错乱。
2. **别名不统一**：合并"推迟到 app 层"，悬空契约。
3. 两库无强约束关联，overlay 靠姓名 join，library 重建与 personal 生命周期不一致。
4. **无迁移机制**：各自 `CREATE TABLE IF NOT EXISTS`，无 `user_version`。
5. Asset/Script/Video 不在库里，落 `storage/tasks/`+json sidecar，无结构化关联；素材无内容哈希、无法去重。
6. `projects` 与"成片"信息分置 JSON 与任务目录，无统一登记（见 §2.3 处理）。

### 1.4 循环依赖（证据）

| 边 | 位置 | 形态 |
|---|---|---|
| library → person_store | [`library.py:337`](app/services/library.py#L337) | 函数内延迟 |
| person_store → library | [`person_store.py:225/323/340/477/563`](app/services/person_store.py#L225)（5 处） | 函数内延迟 |
| biography → library / person_facts | [`biography.py:127/177/202/390`](app/services/biography.py#L127) | 函数内延迟 |
| person_facts → library | [`person_facts.py:275`](app/services/person_facts.py#L275) | 函数内延迟 |
| archives/historical_archive → library | [`historical_archive.py:69/234`](app/services/archives/historical_archive.py#L69) | 函数内延迟 |
| task → biography | [`task.py:346/359/620/1140`](app/services/task.py#L346) | 函数内延迟 |
| controller → library, person_store | [`controllers/v1/biography.py:16`](app/controllers/v1/biography.py#L16) | 顶层（入口层，正常） |

`library ⇄ person_store` 是双向环，全图充斥"延迟 import 破环"。

### 1.5 /api/v1/library/*（11 端点，全在 `controllers/v1/biography.py`）

GET `/library/persons`(:22)/`counts`(:49)/`groups`(:75)、POST `/groups`(:80)/`/persons`(:104)、DELETE `/groups/{name}`(:93)、GET `/persons/{name}/documents`(:122)/`profile`(:151)、PATCH `/persons/{name}`(:168)、PUT `/persons/{name}/groups`(:192)、POST `/persons/{name}/restore`(:210)。`{name}` 为姓名。前端写 [`client.ts:155-191`](frontend/src/lib/api/client.ts#L155)、读 [`queries.ts:88-100`](frontend/src/server/queries.ts#L88)。11 端点全被使用。**尚无 Script/Video/Asset 维度端点。**

---

## 第二部分：统一人物数据模型设计

### 2.1 设计目标

1. 以 **Person 为聚合根**统一 Person / Fact / Asset / Script / Video / Project 引用。
2. **双文件**：`mpt.sqlite`（用户数据，需备份）+ 只读可重建 `library.sqlite`（语料），以 `public_id`/QID/`doc_id` 软引用，不建跨库外键；**史料库缺失一律降级、绝不报错中断**。
3. **稳定代理键 `person_id`（内部整型）+ 不透明对外 `public_id`（ULID + 类型前缀）+ `qid`**。
4. **素材拆分**：文件实体 `assets`（content_hash 唯一）与人-素材关系 `person_assets`。
5. **物化与同步**：史料库人物**惰性物化**为 mpt 记录（见 §2.4）。
6. **一致性**：外键强制、软/硬删除统一语义、`file_missing` + 对账任务、备份自动化。
7. **迁移**：schema 迁移（前滚、启动自动）与一次性数据导入（默认 dry-run）分离。
8. 面向"全景人物页"的 `/api/v2`（不透明 id），向后兼容 `/api/v1/library/*`。

### 2.2 目标实体与字段（`mpt.sqlite`，时间一律 UTC ISO8601）

```sql
PRAGMA user_version;                         -- schema 版本，迁移器读写

CREATE TABLE persons (
  person_id     INTEGER PRIMARY KEY,          -- 内部稳定代理键，不外泄
  public_id     TEXT NOT NULL UNIQUE,         -- 不透明对外 id："per_"+ULID
  qid           TEXT NOT NULL DEFAULT '',     -- Wikidata QID（可空），跨源软锚点
  display_name  TEXT NOT NULL,
  dynasty       TEXT NOT NULL DEFAULT '',
  category      TEXT NOT NULL DEFAULT '',
  note          TEXT NOT NULL DEFAULT '',
  pinned        INTEGER NOT NULL DEFAULT 0,
  origin        TEXT NOT NULL DEFAULT 'library', -- library | custom
  state         TEXT NOT NULL DEFAULT 'active',  -- active | archived（软删默认态）
  no_authentic_photo INTEGER NOT NULL DEFAULT 0, -- 人物级标记：确无本人真实照片
  merged_into   TEXT NOT NULL DEFAULT '',        -- 合并后指向存活方 public_id；''=未合并
  archived_at   TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
-- 注：删除原 slug（全仓 0 使用，见 §E/changelog）。
CREATE INDEX idx_person_state ON persons(state, pinned DESC, display_name);
CREATE INDEX idx_person_merged ON persons(merged_into);

CREATE TABLE person_aliases (               -- 别名/异体/译名/拼音，落地"app 层合并"契约
  person_id INTEGER NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
  alias     TEXT NOT NULL,
  source    TEXT NOT NULL DEFAULT '',       -- wiki | wikidata | manual | pinyin
  PRIMARY KEY (alias, person_id)
);
CREATE INDEX idx_alias_lookup ON person_aliases(alias);   -- 姓名/拼音 → person_id 反查

CREATE TABLE facts (                        -- 结构化事实，可溯源
  fact_id         INTEGER PRIMARY KEY,
  person_id       INTEGER NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
  ordinal         INTEGER NOT NULL,
  statement       TEXT NOT NULL,
  date_text       TEXT NOT NULL DEFAULT '', -- 原文日期文本（"建安二年"）
  year_from       INTEGER,                  -- 年份区间起（可空）
  year_to         INTEGER,                  -- 年份区间止（可空，单点则等于 from）
  place           TEXT NOT NULL DEFAULT '',
  confidence      REAL NOT NULL DEFAULT 0,  -- 0..1
  verify_reason   TEXT NOT NULL DEFAULT '', -- 核验理由/未通过原因
  source          TEXT NOT NULL DEFAULT 'library', -- library | manual | llm
  source_ref      TEXT NOT NULL DEFAULT '', -- doc_id / book_id:idx
  verified        INTEGER NOT NULL DEFAULT 0,
  human_confirmed INTEGER NOT NULL DEFAULT 0,
  edited_by_user  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_facts_person ON facts(person_id, ordinal);

CREATE TABLE assets (                       -- 文件实体（人无关），内容寻址去重
  asset_id      INTEGER PRIMARY KEY,
  kind          TEXT NOT NULL,             -- material|audio|image|thumb（不含成片，见 §2.3）
  path          TEXT NOT NULL,             -- 相对存储根，如 tasks/<id>/shot-0.jpg
  content_hash  TEXT NOT NULL,             -- SHA-256，去重键
  size_bytes    INTEGER NOT NULL DEFAULT 0,
  provider      TEXT NOT NULL DEFAULT '',
  license       TEXT NOT NULL DEFAULT '',  -- ▲ 升为独立列（授权判定基于此）
  license_url   TEXT NOT NULL DEFAULT '',
  author        TEXT NOT NULL DEFAULT '',
  source_page   TEXT NOT NULL DEFAULT '',  -- 来源页 URL
  source_title  TEXT NOT NULL DEFAULT '',  -- 来源页标题
  state         TEXT NOT NULL DEFAULT 'active', -- active | archived | file_missing
  meta_json     TEXT NOT NULL DEFAULT '{}',
  created_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_assets_hash ON assets(content_hash, kind); -- 内容+类型去重
CREATE INDEX idx_assets_state ON assets(state);

CREATE TABLE person_assets (                -- 人 ↔ 素材关系（含选用状态）
  person_id INTEGER NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
  asset_id  INTEGER NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
  state     TEXT NOT NULL DEFAULT 'pending',-- selected | excluded | pending
  is_cover  INTEGER NOT NULL DEFAULT 0,
  role      TEXT NOT NULL DEFAULT '',       -- portrait|scene|document|...
  PRIMARY KEY (person_id, asset_id)
);
CREATE INDEX idx_pa_person_state ON person_assets(person_id, state);

CREATE TABLE scripts (                      -- 一行 = 一份完整结构化脚本快照
  script_id      INTEGER PRIMARY KEY,
  person_id      INTEGER,                   -- 可空：纯主题视频无人物（§A1）
  project_id     TEXT NOT NULL DEFAULT '',  -- 软引用 projects
  task_id        TEXT NOT NULL DEFAULT '',
  kind           TEXT NOT NULL,             -- biography | general（仅此二类）
  version        INTEGER NOT NULL DEFAULT 1,
  prev_script_id INTEGER REFERENCES scripts(script_id),
  body           TEXT NOT NULL,             -- 完整 JSON：hook/timeline[...]/closing + 逐镜↔素材映射
  report_json    TEXT NOT NULL DEFAULT '',  -- 原 script.json.content_report
  created_at     TEXT NOT NULL
);
CREATE INDEX idx_scripts_person ON scripts(person_id, kind, version);

CREATE TABLE videos (                       -- 成片登记（唯一成片登记表，不再与 assets 重复）
  video_id      INTEGER PRIMARY KEY,
  person_id     INTEGER,                    -- 可空：主题视频无人物（§A1）
  project_id    TEXT NOT NULL DEFAULT '',   -- 软引用 projects（§A4）
  script_id     INTEGER REFERENCES scripts(script_id), -- 绑定生成它的脚本版本
  task_id       TEXT NOT NULL,
  path          TEXT NOT NULL,             -- 相对存储根
  content_hash  TEXT NOT NULL DEFAULT '',
  duration_s    REAL,
  state         TEXT NOT NULL DEFAULT 'active', -- active | archived | file_missing
  review_status TEXT NOT NULL DEFAULT 'ok',     -- ok | needs_review（见 §3.4）
  degraded      INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL
);

CREATE TABLE groups (name TEXT PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE group_members (
  group_name TEXT NOT NULL REFERENCES groups(name) ON DELETE CASCADE,
  person_id  INTEGER NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
  added_at   TEXT NOT NULL,
  PRIMARY KEY (group_name, person_id)
);

CREATE TABLE library_links (                -- 与只读 library.sqlite 的软引用（带快照便于重匹配）
  person_id     INTEGER NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
  doc_id        TEXT NOT NULL,
  book_id       TEXT NOT NULL DEFAULT '',
  role          TEXT NOT NULL DEFAULT 'source', -- source|primary_bio
  source_title  TEXT NOT NULL DEFAULT '',   -- 断链重匹配用快照
  source_page   TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (person_id, doc_id)
);

CREATE TABLE review_confirmations (         -- 需确认降级的"我已知晓"留痕（§3.4）
  confirm_id   INTEGER PRIMARY KEY,
  video_id     INTEGER NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
  codes_json   TEXT NOT NULL,
  confirmed_at TEXT NOT NULL
);
```

**FK 一致性说明（§A3）**：`assets` 不再持有 `person_id`（迁至 `person_assets`），因此原 `assets.person_id ON DELETE SET NULL` 与硬删除语义的矛盾消失——`person_assets` 两端均 `ON DELETE CASCADE`；删除某人只清关系行，文件实体 `assets` 由对账任务在**无任何 person_assets 引用且硬删除该素材时**才删。所有子资源整型 id（facts/assets/scripts/videos）在 API 层每次请求都校验"归属于路径中的 public_id"（§E）。

### 2.3 projects / videos / assets 关系（§A4）

- **去掉重复登记**：成片**只登记在 `videos`**；`assets.kind` 取消 `video` 值（只留 material/audio/image/thumb），杜绝"同一 mp4 既是 asset 又是 video"。
- **projects 保持为工作区聚合**（现阶段仍 `projects.json`）：承载 draft 编辑态、status/step 状态机、title、绑定 task_id。`videos.project_id`、`scripts.project_id` 以 `p_xxxx` **软引用** projects，不回写 projects.json，避免双写。
- **persons ↔ projects**：projects.person 现为姓名；导入时经别名解析为 `videos.person_id`（可空）。人物全景页的"本片属于谁"用 `videos.person_id`，"作品列表"仍以 projects 为入口。
- **沿用不变**：作品列表 / 重命名 / 重新生成 / 删除继续走 [`project_store`](app/services/project_store.py#L76)（响应与语义零变化）；新增的只是"成片可选地反查 person_id / 脚本版本"。**把 projects 整体并入 mpt.sqlite 不在本阶段范围**（列后续子设计）。

### 2.4 物化与同步（§B5，采纳惰性物化）

**eager vs lazy 对比：**

| 维度 | eager（启动即全量灌 library.persons 进 mpt） | lazy（按需物化，推荐） |
|---|---|---|
| 首启/迁移成本 | 高（数千行预写） | 低（仅用户触达者入库） |
| library 重建 | 需重放全量、易与用户数据冲突 | 只影响"候选视图"，mpt 不动 |
| 用户数据面 | mpt 行数膨胀、含大量未用条目 | mpt 只含用户真正用过的 |
| 列表实现 | 单表查询 | **并集视图**（library 候选 + mpt 已物化） |

**建议 lazy**：史料库人物以"**候选**"出现在列表；仅当被**使用/编辑/生成**时才 `INSERT` 进 `mpt.sqlite`（`origin='library'`）。列表 = `library.persons`（投影）∪ `mpt.persons`（state=active），按 display_name 去重，mpt 记录优先。

**重建不覆盖用户覆盖字段（关键保证）**：library.sqlite 重聚合只改 library 侧候选；mpt 中 `origin='library'` 行的 dynasty/category/note/pinned/事实/素材关系等**用户可编辑列永不被自动写回**。提供显式 `refresh_from_library(person_id)` 命令，仅更新结构派生列（如新 doc_id 关联），且对 `edited_by_user=1` 的字段跳过。

### 2.5 /api/v1 兼容 与 /api/v2（§二.2 + §E）

- **`/api/v2` 与详情页一律用不透明 `public_id`**（`per_<ULID>`）；**同进程、独立 router 包** `app/controllers/v2/`（挂 `/api/v2`），与 v1 共存。
- **旧 `/api/v1/library/*` 冻结并 `deprecated: true`**：`{name}` 继续接受姓名/别名/拼音，controller 经 `person_aliases` 解析 `public_id` 再走新库；响应保持现字段（可增量加 `public_id`）。
- v2 全景端点：`GET /api/v2/persons/{public_id}/overview` 及 `facts|assets|scripts|videos` 分项；合并/重定向时旧 public_id 经 `merged_into` 返回 3xx/新 id。
- 搜索：`display_name`/`person_aliases.alias`（含 `source='pinyin'`）LIKE/前缀匹配（§B7）。

### 2.6 交付顺序（步骤 1、2 已放行；3+ 待 dry-run 报告后决定）

1. **【放行】新增 `person_repo.py` + `db_migrations.py` 骨架 + M0 建表**（纯新增，不改现有行为，不接线到 controller）。
2. **【放行】一次性数据导入命令**（默认 dry-run）+ 对拍报告 + **合成夹具测试**。跑真实副本出 dry-run 报告后**停下汇报**。
3. controller 切 person_repo（v1 保契约 + 别名解析；引入 v2）。
4. 摘除 `library⇄person_store` 反向 import，收敛依赖图。
5. v2 overview 端点 + 前端全景页。
6. `storage_source` 灰度 → 默认单库 →（独立删除提交）移除旧读写路径。

### 2.7 并发、删除与一致性（§A3 + §B8 + §E）

- **连接工厂（`person_repo` 内唯一入口）**：每个连接执行 `PRAGMA foreign_keys=ON; PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=30000;`。**测试断言级联生效**（建父子行、删父、断言子行随 `ON DELETE CASCADE` 消失）。
- **并发**：**`BEGIN IMMEDIATE` + 短事务**；连接**不跨线程共享**（每线程/任务独立连接，函数内即用即关）；写经单写者序列化避免 `SQLITE_BUSY`，读并发（WAL）。
- **删除语义统一**：
  - **软删除（默认）**：仅改 `state='archived'`（person/assets/videos 皆然），列表/概览过滤掉，文件保留。
  - **硬删除（二次确认后）**：单事务内 `DELETE` 库行（子表随 CASCADE），提交后**再删磁盘文件**；若删文件阶段崩溃，遗留孤儿文件由对账任务清理。
- **一致性（§B8）**：`assets.state/videos.state` 增 `file_missing`；**对账任务**（`person_repo.reconcile()`，CLI/后台）扫描：库指文件不存在 → 标 `file_missing`；文件存在但无库引用且超出保留窗 → 报告/清理。**所有存储清理只经 `person_repo`**，杜绝旁路删文件。
- **备份**：迁移 `apply` 前自动执行 `VACUUM INTO '<ts>.mpt.sqlite'`（或 `sqlite3 .backup`）落**非云同步目录**；对账与删除任务前对将删文件可选保留快照。

### 2.8 迁移与回滚（§C9 + §C10）

**两类操作分离：**

1. **Schema 迁移**（`db_migrations.py`，`PRAGMA user_version`）：**启动时自动、只前滚、不提供 down**（前滚为加表/加列等幂等安全操作）。M0=建表。
2. **一次性数据导入**（CLI `python -m app.services.import_library`，**默认 dry-run**）：M1 数据搬运。`--apply` 才写库，且 apply 前先自动备份。
   - 步骤：`library.persons`→`persons(origin=library)`+`person_aliases`+`library_links(带 title/page 快照)`+`facts(source=library, verified=0)`；`personal.person_meta`→合并覆盖字段，`custom=1`→`origin=custom`，`hidden=1`→`state=archived`，手工事实→`source=manual, human_confirmed=1, edited_by_user=1`；**overlay "替换"语义下不再导入被替换的库事实**（§B6）；`groups/group_members`→姓名解析 person_id；扫 `storage/tasks/*` 与 `projects.json` 回填 `videos/scripts`（算 content_hash、路径相对化、绑 project_id，匹配不到则 `person_id NULL` 入"无法映射清单"）。

- **dry-run 对拍报告**：各表迁移前后条数（persons/facts/assets/person_assets/scripts/videos/groups/aliases/links）+ **无法映射记录清单**（逐条原因）+ content_hash 去重命中数 + merged/重名提示。
- **回滚（明示）**：**不做双写**；回滚窗口 = "默认切到 mpt"之前，此阶段删 `mpt.sqlite` 即回到双库读写；**切换后再回滚会丢失切换期间的新写入**（mpt 为唯一真源），文档与 CLI 提示均明示此风险，切换点前强制备份。
- **测试夹具（§C10）**：迁移测试用**合成数据**（脚本生成 persons/facts/person_meta/任务目录），或仅保留**人物名与 id 结构**；**严禁把真实语料正文/书籍内容提交进仓库**。CI 与本地夹具测试均在 tmp 目录构造假库。

---

## 第三部分：流水线降级可见化机制

### 3.1 对异常审计的回应（含 §D13 处置）

**核实更正**：[`task.py:544-547`](app/services/task.py#L544) 的 `try` **只包裹 `from app.services import content_check`**，检查体 [`:550`](app/services/task.py#L550) 起在 try 外、异常正常上抛——原"检查体被静默吞掉"表述不准确，已更正（见前述）。

**进一步核实（§D13 依据）**：[`content_check.py`](app/services/content_check.py#L1) **只 import `re`/`typing`**，无可选/重型依赖，import 不可能失败。故该 try/except 是**多余的防御**。**处置：删除 try/except，改顶层 `from app.services import content_check`**；import 失败若真发生属部署级严重错误，应直接暴露而非静默降级。`content_check_skipped` 降级**仅保留给"用户在配置里主动关闭内容闸门"** 的情形。

### 3.2 降级码注册表 + 任务级收集器（§D11 + §D12）

**严重度模型（§D11）**：`severity ∈ {info, warn}`；**另设正交标志 `requires_ack`**（是否需用户显式确认才可导出），不再用 severity=error/compliance 布尔表达拦截。

**降级码注册表**（单一真源，新增 `app/services/degradations.py`）：

```python
# code → 元数据；合规/拦截属性由注册表派生，调用点只传 code + context。
DEGRADATION_REGISTRY: dict[str, DegradationSpec] = {
  "ai_label_burn_in_failed":    Spec(stage="compliance", severity="warn", requires_ack=True,  i18n_key="degradation.ai_label_burn_in_failed"),
  "license_info_missing":       Spec(stage="material",   severity="warn", requires_ack=True,  i18n_key="degradation.license_info_missing"),
  "quality_gate_not_passed":    Spec(stage="compose",    severity="warn", requires_ack=True,  i18n_key="degradation.quality_gate_not_passed"),
  "content_check_skipped":      Spec(stage="content_gate",severity="warn",requires_ack=True,  i18n_key="degradation.content_check_skipped"), # 仅"用户关闸门"
  "library_unavailable":        Spec(stage="material",   severity="warn", requires_ack=False, i18n_key="degradation.library_unavailable"),
  "target_seconds_unavailable": Spec(stage="content_gate",severity="info", requires_ack=False, ...),
  "grounding_unavailable":      Spec(stage="script",     severity="warn", requires_ack=False, ...),
  "per_shot_abandoned":         Spec(stage="shot_audio", severity="warn", requires_ack=False, ...),
  "material_fetch_failed":      Spec(stage="material",   severity="warn", requires_ack=False, ...),
  "image_probe_failed":         Spec(stage="compose",    severity="info", requires_ack=False, ...),
  # …
}

def record(code: str, *, context: dict | None = None, task_id: str = "") -> None:
    """调用点唯一入口：只给 code(+可选 context)。未知 code 立即 raise（注册表即契约）。"""
```

**收集方式（§D12）**：**任务级收集器**，基于 **`contextvars`**（每任务一个 `ContextVar[DegradationCollector]`），阶段边界 `except Exception` 捕获后调 `record(code, context=...)` 落入当前任务收集器；任务结束时把 `degradations[]` 写 result / `script.json` / `quality_report` / `*.compliance.json`，并置 `videos.degraded` / `review_status`。
- **线程池场景**：worker 线程须用 `contextvars.copy_context()` 包裹任务体，确保收集器上下文随任务传播、不串台。
- **聚合**：同一 `code` 只保留一条，`count += 1`，`context_samples` **仅存前 N 条**（默认 3）样例，避免刷屏与体积膨胀。

**落盘 & 展示**：结果页/确认屏读 `degradations[]`，按 `severity` 呈现（info 折叠、warn 徽标），`requires_ack=True` 顶部横幅 + 确认闸；文案走 `i18n_key` 映射。

### 3.3 A 类 8 处按注册表归类（C 类汇入同一 record 口）

| 位置 | 处置 | code | severity | requires_ack |
|---|---|---|---|---|
| [`task.py:544`](app/services/task.py#L544) | **删 try/except→顶层 import**（§D13），不再作降级 | —（不再是降级） | — | — |
| [`task.py:529`](app/services/task.py#L529) | target_seconds→0 | target_seconds_unavailable | info | ✗ |
| [`biography.py:112`](app/services/biography.py#L112) | grounding={} | grounding_unavailable | warn | ✗ |
| [`historical_archive.py:239`](app/services/archives/historical_archive.py#L239) | grounding_block 丢失 | grounding_block_lost | warn | ✗ |
| [`shot_audio.py:121`](app/services/shot_audio.py#L121) | 放弃逐镜音频 | per_shot_abandoned | warn | ✗ |
| [`video.py:757`](app/services/video.py#L757) | orientation/image_mode 静默 | image_probe_failed | info | ✗ |
| [`material.py:1421/1429`](app/services/material.py#L1421) | 素材缺失 | material_fetch_failed | warn | ✗ |
| （§二.1） | 史料库缺失→空语料不报错 | library_unavailable | warn | ✗ |

C 类 warning-only 统一改为阶段边界 `record(code, context)`，取代散落 `logger.warning` 即丢弃。正例范式 [`video.py:1853`](app/services/video.py#L1853)（BGM 混音失败显式置标志）。

### 3.4 需确认降级闸（§二.3，注册表派生）

**`requires_ack=True` 的合规类码清单**（由注册表派生，非硬编码布尔）：
`ai_label_burn_in_failed`、`license_info_missing`、`quality_gate_not_passed`、`content_check_skipped`。

**行为**：命中任一 → `videos.review_status='needs_review'`（不阻断生成，阻断静默导出）；结果页展示且**用户点"我已知晓"方可导出**，确认写 `review_confirmations`（video_id + codes_json + 时间）并回写 sidecar；`license_info_missing` 判定基于 `assets.license/author/source_page` 列是否为空（§A2）；其余降级只提示不拦截。

**编码原则**：`except Exception` 只在阶段边界且必 `record`；阶段内只捕具体异常。门禁 ruff `BLE001/S110/S112`，存量 per-file 基线 + `# noqa`（附因），新增代码严格执行。

---

## 附：本次修订 changelog（对应审阅意见逐条）

**A. 内部矛盾**
- **A1**：`scripts.person_id`、`videos.person_id` 改为**可空**（主题视频/无法映射产物）；§2.2 去掉其 `NOT NULL`，§2.8 M1 "匹配不到 → person_id NULL 入无法映射清单" 与之一致。
- **A2**：**拆分 `assets`（文件实体，`content_hash` 唯一）+ `person_assets`（state: selected|excluded|pending、is_cover、role）**；license/license_url/author/source_page/source_title **升为 assets 独立列**，`license_info_missing` 基于这些列判定；"此人无真实照片"作 `persons.no_authentic_photo` 人物级标记。
- **A3**：§2.7 连接工厂统一 `PRAGMA foreign_keys=ON` + 断言级联的测试；删除语义统一（软删只改 state；硬删事务内删行→提交后删文件→崩溃由对账清孤儿）；`assets` 去 `person_id`，SET NULL 与 §2.7 矛盾消除（改由 `person_assets` 双 CASCADE）。
- **A4**：核实 projects 为 `projects.json`（§1.2）；成片只登记 `videos`、`assets.kind` 取消 `video`（去重）；`videos/scripts.project_id` 软引用 projects；作品列表/重命名/重新生成沿用 project_store 不变。

**B. 缺口**
- **B5**：§2.4 新增物化与同步，**采纳惰性物化**（eager/lazy 利弊对比表），列表为并集视图；明确 library 重建不覆盖用户可编辑列，提供 `refresh_from_library` 且跳过 `edited_by_user=1`。
- **B6**：`facts` 增 `date_text/year_from/year_to/place/confidence/verify_reason`；M1 中 library 事实默认 `verified=0`、个人手工事实 `source='manual'/human_confirmed=1/edited_by_user=1`、替换语义下不导入被替换库事实。
- **B7**：`persons.merged_into`（旧 public_id 重定向）；搜索支持拼音（`person_aliases.source='pinyin'`）；合并语义（迁移 facts/person_assets/person_assets/library_links/groups 关系、冲突以存活方为准、被并方 state=archived+merged_into）写入 §2.2/§2.5。
- **B8**：`file_missing` 状态 + `person_repo.reconcile()` 对账任务；存储清理只经 person_repo；备份 `VACUUM INTO`/`.backup` 于迁移 apply 前自动执行。

**C. 迁移与回滚**
- **C9**：§2.8 拆"**schema 迁移**（启动自动、只前滚、不写 down）"与"**一次性数据导入命令**（默认 dry-run）"；回滚改为"不双写、apply 前自动备份、回滚窗口=切换前、切换后回滚丢新写入"并明示。
- **C10**：迁移测试夹具用合成数据或仅名/ID 结构，**禁提交真实语料正文**。

**D. 降级机制**
- **D11**：严重度 `info|warn` + 正交 `requires_ack`；新建**降级码注册表**（`app/services/degradations.py`：code→stage/severity/requires_ack/i18n_key），合规/拦截属性由注册表派生，调用点只传 code+context。
- **D12**：收集改**任务级 contextvars 收集器**，线程池用 `copy_context`，同 code 聚合计数、context 仅存前 N 样例。
- **D13**：task.py:544 的 try/except **删除、改顶层 import**（content_check 仅依赖 re/typing，import 不会失败）；`content_check_skipped` 仅用于用户主动关闸门。

**E. 小点**
- `scripts`：一行一份完整结构化脚本快照，`kind` 仅 biography|general，镜头↔素材映射存 `body`，成片绑 `script_id`。
- 时间统一 **UTC ISO8601**；并发定为 **BEGIN IMMEDIATE + 短事务 + 连接不跨线程共享**。
- **删除 `slug`**（全仓 0 使用）。
- 子资源整型 id 可用，但每次请求校验归属于路径 public_id。
- 开放问题定案：`public_id = per_<ULID>`（类型前缀）；`library_links` 增 `source_title/source_page` 快照用于断链重匹配；`/api/v2` 同进程、独立 router 包 `app/controllers/v2/`。
