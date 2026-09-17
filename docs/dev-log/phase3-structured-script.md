# Phase 3 — 结构化传记脚本 + 事实校验

## 交付
`app/services/biography.py`
- `build_fact_material(person)`:事实底座 = 本地史料库(人物档案+书目) + Wikipedia 摘要;
  语料 < 600 字时自动从已索引书籍中抽取**含年份的段落**(每条截断 200 字,
  仅作事实参考,严禁原文引用)补足。实测爱因斯坦语料 260 → 1278 字。
- `generate_structured_script(person)`:输出严格 JSON
  `hook / timeline[{narration, duration_hint, visual_type, visual_query}] / closing`,
  非法 visual_type 自动归一到 historical_photo,时长夹在 4-30s,重试 3 次。
- `verify_shots(shots, corpus)`:轻量事实校验,逐分镜核对年份与地点是否在语料中。
  - 年份:命中语料、或为语料年份 ±1(史料 1904/1905 之争)即通过
  - 语料过薄(<600 字)时降级为「需人工复核」而非断言无依据,避免误伤正确史实
  - 地点:带停用词/动词标记/时段后缀三重过滤,消除「一个不断更换国」「年美国」类正则噪声
  - 未通过的分镜写 `verified: false` + `unverified_reasons`,不阻断生成
- `script_to_narration / script_to_search_terms / script_shot_visual_types`:
  供下游分别驱动 TTS、素材检索、素材调度,三者顺序严格一致。

## 接入点
- `task.py::generate_script`:historical_archive 源或有 biography_person 时先生成结构化脚本;
  失败自动回落到原版脚本 prompt(不影响非传记任务)
- `task.py::generate_terms`:直接复用脚本的 visual_query(省一次 LLM 调用,保证画面顺序=叙事顺序)
- `task.py::_shot_visual_types`:把分镜 visual_type 传给素材调度
- `task.py`:结构化脚本在 `save_script_data` **之后**写入(该函数整体覆写任务清单)

## 素材质量修复(实测发现)
Commons 搜索会把 PDF、地图、以及「以人名命名的街道/中学/纪念碑」照片当作命中结果。
`archives/wikimedia.py` 新增 `_is_noise_asset`:
- 非图片扩展名(.pdf/.djvu/.svg/.map)直接剔除
- 文件名含 straße/school/gymnasium/memorial/statue/building/park 等 30+ 词根剔除
实测:`Albert Einstein portrait` 结果全部为本人真实照片(人像/护照/童年照)。

## 端到端验证(任务 40db6227)
- 成片 232.5s(3分52秒),1920×1080,85MB
- script.json 落盘:biography_script(15 分镜)+ biography_facts_check + biography_person
- 事实校验:checked=5, unverified=0(语料 1278 字,非薄语料)
- 分镜视觉类型分布:historical_photo / generated_scene / b_roll 三种均被正确产出与调度
- 素材溯源 9 条,全部 wikimedia 且为 PD/CC0(合规)
