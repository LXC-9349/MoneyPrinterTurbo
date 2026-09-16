# Phase 2 前置 — 传记史料库（电子书整理 + 朝代时间线索引）

数据源（只读，未改动源文件）：
- A 库 `/mnt/d/ai/中国名人古今人物传记`：4942 篇 GB18030 .doc（5 主题分类）
- B 库 `/mnt/d/ai/人物传记合集`：1256 本 mobi/epub/azw3（含 2 本 NCX 声明编码错误，已容错修复）

产物：`storage/library/library.sqlite`（约 914MB，gitignored）
- documents(4942) + chapters(书籍全文) + books(1256 done)
- docs_fts / books_fts：FTS5 contentless，CJK 逐字分词；查询用空格短语保证相邻匹配
- annotations：LLM 标注（persons/dynasty/years/doc_type/key_facts/reliable）
- persons：按传主聚合档案（标注全部完成后跑 aggregate_persons.py 生成）
- dynasty_timeline：scripts/library/data/dynasty_timeline.json（朝代↔公元年↔英文 era 词）

消费层：`app/services/library.py`
- get_person_profile(name) / search_facts(q) / search_book_excerpts(q) /
  dynasty_of / era_keywords / grounding_block(name)
- 全部只读、任何故障返回空（不阻断生成主链路）
- 版权红线：只回传 ≤160 字 snippet + 事实要点，绝不向 prompt 输出成段原文

离线流水线（scripts/library/）：
  common.py → ingest_docs.py → ingest_books.py → build_fts.py
  → annotate.py --target docs/books（断点续跑）→ aggregate_persons.py
  → quality_report.py（异常书检核）

质量验证：
- B 库 1256/1256 成功，抽样告警 27 本经人工复核均为误报（书尾为版权目录页）
- 检索探针："曹操 官渡" 26 书 / 55 文；"拿破仑 滑铁卢" 46 书 / 4 文
- 标注抽检 25 篇：传主/朝代/年份/doc_type/reliability 与人工判断一致
