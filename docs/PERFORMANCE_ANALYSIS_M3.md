# 渲染性能分析报告（里程碑 3 #1）

日期：2026-10-15 ｜ 环境：本机 dev API（uvicorn 8917，热重载）+ Windows 本地 ffmpeg（libx264）

## 一、采集机制

- 流水线在每个阶段边界（`_raise_if_cancelled` 调用点 + 质检/合规显式边界）结算上一阶段
  耗时，**双写**：
  - `storage/tasks/<task_id>/stage_timings.json`（文件，崩溃前最后边界可靠）；
  - `tasks.stage_timings`（schema v4 新列，JSON map `{stage: 秒}`，增量合并写），
    供任务详情页阶段时间线（M3#3）直接读表。
- 覆盖阶段：`preflight / script / terms / audio / subtitle / materials / video / quality / compliance`。
- 失败与取消同样保留已结算的部分耗时（本次采集中 audio 失败任务即如此）。
- 计时是纯观测配角：任何落盘/写表失败只记日志，绝不阻断生成。

## 二、真实任务实测

### 任务 1（LLM 生成脚本，audio 阶段因 TTS 网络瞬时失败）

| 阶段 | 耗时 |
|---|---|
| preflight | 0.06s |
| **script（LLM）** | **264.38s** |
| terms | 6.36s |
| audio | 失败（edge-tts 连接瞬时不通，非性能样本） |

### 任务 2（显式脚本+显式 terms，90 秒级成片，端到端完整成功）

| 阶段 | 耗时 | 占比 |
|---|---|---|
| preflight | 0.08s | 0% |
| script | 0.02s | 0% |
| terms | 0.02s | 0% |
| audio（edge-tts 90s 旁白） | 9.50s | 1.1% |
| subtitle | 0.03s | 0% |
| materials（pexels 下载） | 40.91s | 4.9% |
| **video（合成）** | **788.61s** | **93.6%** |
| quality（ffprobe 逐成片） | 0.30s | 0% |
| compliance（sidecar） | 0.00s | 0% |
| **合计** | **≈840s**（轮询墙钟 842.2s，吻合） | |

成片 90.5s / 1920x1080 / 30fps；素材源 pexels（多为 4K 原片）。

## 三、瓶颈结论

1. **video 合成独占 93.6%**，内部分三段（日志核对）：
   - 逐片段解码 4K 原片 → 缩放 1080p → **libx264 medium 整段重编码**（每片段一次完整编码，
     是本阶段的大头）；
   - ffmpeg concat 拼接（`-c copy`，近零成本）；
   - 最终混音输出（BGM/旁白/字幕叠加再编码一次，**`threads=params.n_threads or 2`
     被钉死 2 线程**，libx264 2 线程在 1080p30 上慢 3~4 倍）。
   - 整条链路成片内容被**完整重编码 2 次**（片段级 + final 级）。
2. **script（LLM）是网络侧方差瓶颈**：单请求已有 90s 超时（M2#8），但
   `generate_script` 内置最多 5 次重试，串行最坏 ≈450s 才落空；且当前 config.toml
   **未配置 `fallback_llm_provider`**，备用切换通道虽已接线但现场不生效。
3. materials 40.9s 为 pexels 下载带宽成本，Pexels API 支持按分辨率筛选。

## 四、目标核对与最划算优化

**目标：90 秒成片端到端 ≤5 分钟（300s）。当前 840s，超 2.8 倍。**

按性价比排序（不改成片质量语义）：

| # | 优化 | 改动量 | 预期 |
|---|---|---|---|
| 1 | 最终混音输出放开 2 线程钉死：`threads` 取 `os.cpu_count` 与配置上限的合理值（片段级编码同样受益） | 极小（video.py 一处） | final 段 3~4×；总时长 → ~400s |
| 2 | pexels 检索按目标分辨率选素材（`size=large` 而非原片 4K），解码量降 ~4× | 小（material.py 查询参数） | 片段编码段 ~2× 收益；总时长 → ~300s |
| 3 | 片段级编码 preset 下调（medium→veryfast）或片段级并发按 CPU 核数提升（现为 `n_threads` 相关配置） | 小（可配置项） | 再 -30~50s |
| 4 | LLM：脚本阶段加**总预算**（如 150s，跨重试共享），超时即切 fallback；config 补 `fallback_llm_provider`（groq 已配置可用） | 小（llm.py 预算参数） | 消除 264s 方差，最坏 → 150s |
| 5 | 架构级（后续）：片段处理与 final 合并为单次编码管线 | 大 | 长期最优，本轮不做 |

只做 #1 即可把典型任务压进 ~7 分钟；**#1+#2 组合预计达成 ≤5 分钟目标**（340+150 素材
下载不变、编码段合计 ~150s → ≈300s）。#1/#2 的落地调优属 M3 后续批次，本报告先固化
数据与结论；插桩本身（表列/迁移/双写/测试）已随本提交落地。

## 五、数据留存

- 任务 1：`74f2c876-9888-49cc-8ca5-e3df5b0d7a50`（failed@audio，部分耗时已落表）
- 任务 2：`463e3e04-9fa5-41c7-93f5-70bac906e4f4`（completed，全量耗时落表）
- 轮询墙钟日志：`storage/_m3run.log`、`storage/_m3run2.log`；采集快照：`storage/_m3_perf.json`
- 两任务均未登记成片/素材（非人物中心任务，符合 M2#1/#2 的登记条件设计）。
