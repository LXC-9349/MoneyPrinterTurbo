"""任务取消控制（里程碑 3 #6）。

背景：阶段边界协作式取消（``task._raise_if_cancelled``）只在阶段切换时读取
取消标记，长阶段内部——LLM 调用、单次 TTS、ffmpeg 拼接、逐片段合成——会一直
阻塞到该阶段结束才响应，取消一条正在拼接的视频可能要等满
``ffmpeg_concat_timeout_seconds``。本模块补两件事：

1. **进程内取消事件 + 子进程登记表**：取消接口在写 ``cancel.json`` 标记的同
   时置事件并终止该任务正在运行的 ffmpeg 子进程，使长阶段在毫秒级停下，不留
   孤儿编码进程。
2. **阶段内部子单元检查**：流水线在逐片段合成、ffmpeg 拼接轮询等循环里调用
   :func:`check` / :func:`is_cancelled`，命中即抛 :class:`TaskCancelled`，由
   ``task.start()`` 收敛到 cancelled 终态。

跨进程可靠性仍以 ``cancel.json`` 文件为准（worker 可能跑在别的进程）；事件与
登记表是当前进程的快速通道，二者互补。

取消响应时间（近似上界，详见 ``docs/CANCELLATION.md``）：
- 阶段边界：立即——下一个边界即读取标记 / 事件。
- ffmpeg 拼接：≤ 轮询间隔（默认 0.5s）+ terminate/kill 收敛（grace 3s）。
- 逐片段合成：≤ 一个片段的编码耗时（4K 片段约十几秒）。
- LLM 脚本、单次 TTS、单次 MoviePy ``write_videofile`` 这类不可中断的原子调
  用：取消在该调用返回后立即生效，上界为各自的超时——脚本 LLM 默认 90s，
  TTS / 最终渲染取决于音频时长与编码器吞吐。
"""

from __future__ import annotations

import threading
import time
from contextvars import ContextVar

# 当前执行线程正在处理的任务 id。流水线在 worker 线程里顺序推进，
# contextvar 天然按线程隔离，并发多任务时互不串扰。
_current_task: ContextVar[str] = ContextVar("mpt_current_task", default="")

# ffmpeg 子进程轮询取消的默认间隔（秒）。
DEFAULT_POLL_INTERVAL_SECONDS = 0.5
# terminate 后等待优雅退出的宽限（秒），超时强杀。
DEFAULT_TERMINATE_GRACE_SECONDS = 3.0

_lock = threading.RLock()
# task_id → 取消事件。信号置位即“该任务已被请求取消”。
_events: dict[str, threading.Event] = {}
# task_id → 正在运行的子进程句柄集合（Popen）。
_procs: dict[str, set] = {}


class TaskCancelled(Exception):
    """长阶段内部检查命中取消时抛出；由 ``task.start()`` 收敛为 cancelled 终态。

    与阶段边界的 ``_TaskCancelled`` 不同：本异常从被调用的服务层（video 拼接 /
    逐片段循环）向上抛，携带 ``stage`` 供终态记录失败发生在哪个阶段。
    """

    def __init__(self, task_id: str = "", stage: str = ""):
        super().__init__(f"task cancelled in stage: {stage or 'unknown'} ({task_id})")
        self.task_id = task_id
        self.stage = stage


def set_current_task(task_id: str) -> None:
    """绑定当前线程正在处理的任务，供下游无 task_id 参数的服务读取。"""
    _current_task.set(task_id or "")


def get_current_task() -> str:
    return _current_task.get()


def activate(task_id: str) -> None:
    """流水线开始时登记取消事件（幂等）。"""
    if not task_id:
        return
    with _lock:
        _events.setdefault(task_id, threading.Event())


def deactivate(task_id: str) -> None:
    """流水线结束时回收事件与子进程登记，避免字典无界增长。"""
    with _lock:
        _events.pop(task_id, None)
        _procs.pop(task_id, None)


def get_event(task_id: str = "") -> threading.Event | None:
    tid = task_id or _current_task.get()
    if not tid:
        return None
    with _lock:
        return _events.get(tid)


def is_cancelled(task_id: str = "") -> bool:
    """当前（或指定）任务是否已被请求取消——仅查进程内事件，快、无 IO。"""
    tid = task_id or _current_task.get()
    if not tid:
        return False
    with _lock:
        ev = _events.get(tid)
    return bool(ev is not None and ev.is_set())


def signal(task_id: str) -> None:
    """置取消事件：取消接口写标记文件的同时调用，使本进程的快速通道立即生效。"""
    with _lock:
        ev = _events.get(task_id)
    if ev is not None:
        ev.set()


def register_proc(proc, task_id: str = "") -> None:
    """登记一个正在运行的子进程，使其可被取消立即终止。"""
    tid = task_id or _current_task.get()
    if not tid or proc is None:
        return
    with _lock:
        _procs.setdefault(tid, set()).add(proc)


def unregister_proc(proc, task_id: str = "") -> None:
    tid = task_id or _current_task.get()
    if not tid or proc is None:
        return
    with _lock:
        bucket = _procs.get(tid)
        if bucket:
            bucket.discard(proc)


def running_procs(task_id: str) -> list:
    with _lock:
        return list(_procs.get(task_id, ()))


def terminate_tree(task_id: str, grace_seconds: float = DEFAULT_TERMINATE_GRACE_SECONDS) -> int:
    """置取消事件并终止该任务在跑的子进程，返回被终止的进程数。

    先 :meth:`Popen.terminate` 留 ``grace_seconds`` 优雅退出，超时再 :meth:`kill`
    强杀，绝不留下孤儿编码进程。取消接口据此把“正在拼接”的任务在毫秒级停下，
    而不必等 ffmpeg 自然跑完或触发自身超时。
    """
    signal(task_id)
    procs = running_procs(task_id)
    terminated = 0
    for p in procs:
        try:
            if p.poll() is None:
                p.terminate()
                terminated += 1
        except Exception:  # pragma: no cover - 句柄已失效不影响后续
            pass
    deadline = time.monotonic() + grace_seconds
    for p in procs:
        while p.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if p.poll() is None:
            try:
                p.kill()
            except Exception:  # pragma: no cover - 强杀失败已尽力
                pass
    return terminated


def check(task_id: str = "", stage: str = "") -> None:
    """长阶段内部子单元边界检查：命中取消抛 :class:`TaskCancelled`。"""
    tid = task_id or _current_task.get()
    if tid and is_cancelled(tid):
        raise TaskCancelled(tid, stage)
