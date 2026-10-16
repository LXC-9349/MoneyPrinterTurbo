# 任务取消语义（里程碑 3 #6）

本文说明一条进行中的任务被“取消”后，系统在各阶段如何响应、多久停下，以及
ffmpeg 子进程如何被真正终止。对应实现：`app/services/cancel.py`、
`app/services/task.py`、`app/services/video.py`、`app/controllers/v1/workbench.py`。

## 两层取消

1. **阶段边界（协作式，跨进程可靠）**——取消接口写 `cancel.json` 标记 + 置内存态，
   流水线在每个阶段开始前调用 `task._raise_if_cancelled()` 读取该标记，命中即固定
   `cancelled` 终态、清理临时产物并停线，不再进入下一阶段的 LLM/TTS/素材/合成。

2. **长阶段内部（进程内快速通道，里程碑 3 #6 新增）**——同一请求路径
   `workbench._request_cancel()` 在写标记的同时：
   - `cancel.signal(task_id)`：置该任务的进程内取消事件；
   - `cancel.terminate_tree(task_id)`：终止该任务正在运行的 ffmpeg 子进程。

   流水线在阶段内部的子单元边界（逐成片循环、逐片段合成批、ffmpeg 拼接轮询）
   调用 `cancel.check()` / `cancel.is_cancelled()`，命中即抛 `TaskCancelled`，由
   `start()` 收敛为 `cancelled`（**不会**被误记成 `failed`）。

`cancel.json` 文件是权威依据（worker 可能跑在别的进程，Redis 队列亦然）；事件与
子进程登记表是当前进程的快速通道，二者互补。

## 各阶段取消响应时间（近似上界）

| 阶段 / 操作 | 是否可中断 | 取消响应上界 |
|---|---|---|
| 阶段边界（进入下一阶段前） | 是 | 立即——下一个边界即读标记/事件 |
| ffmpeg 拼接 `concat` | **是，子进程被 terminate→kill** | ≤ 轮询间隔（默认 0.5s）+ 终止收敛（grace 3s） |
| 逐片段合成（每条成片 / 每批片段前检查） | 是 | ≤ 一个片段的编码耗时（4K 片段约十几秒） |
| LLM 脚本生成 | 否（单次 HTTP 调用不可中断） | 该调用返回后立即生效；上界 = 超时（默认 90s，可配 `llm_timeout_seconds`） |
| 单次 TTS 配音 | 否（原子调用） | 调用返回后在下一检查点生效；取决于音频时长 |
| 单次 MoviePy `write_videofile` 最终渲染 | 否（原子调用） | 本次渲染返回后生效；取决于时长与编码器吞吐 |

要点：**能安全终止的外部进程（ffmpeg）一律 terminate→kill，不留孤儿编码器**；
无法安全打断的原子网络/编码调用（LLM、单次 TTS、单次 write_videofile）在其自然
结束点收敛，取消绝不会被“吞掉”——要么在阶段边界，要么在阶段内部检查点。

## ffmpeg 子进程终止

- `_run_concat_with_heartbeat` 用 `subprocess.Popen` 启动，句柄登记到
  `cancel.register_proc(proc)`；主循环以 `communicate(timeout=poll)` 轮询，命中取消
  或到达 `ffmpeg_concat_timeout_seconds` 即 `_terminate_process(proc)`（先 `terminate()`
  留 grace，超时 `kill()`），再抛 `TaskCancelled` / `TimeoutError`。
- 取消接口通过 `terminate_tree` 直接终止登记表里的同一句柄，因此**即使流水线正
  阻塞在 `communicate` 里，子进程也会被外部杀掉**，随后 `Popen.returncode != 0` 且
  `is_cancelled()` 为真时按取消处理，不误判为拼接失败。
- `task.start()` 的 `finally` 调 `cancel.deactivate(task_id)` 回收事件与句柄集合，
  登记表不随任务数无界增长；`resume` 续跑前先 `deactivate` 卸掉可能残留的已置位事件。

## 配置

- `app.cancel_poll_interval_seconds`（默认 `0.5`）：ffmpeg 拼接轮询取消的间隔，最小 0.1s。
- `app.ffmpeg_concat_timeout_seconds`（默认 `3600`）：拼接硬超时，超时同样终止子进程。
