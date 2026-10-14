"""工作区重构后的 AppTest 测试辅助。

Phase A/C 起 `webui/Main.py` 以项目为中心：未打开项目时只渲染项目列表，配置面板
（脚本/视频/音频/字幕/生成）改为在打开的项目工作区里按步渲染。因此过去直接
`AppTest.from_file(Main.py)` 就能拿到面板控件的测试，现在必须"打开一个项目并定位
到目标步骤"才会渲染对应面板。

本模块提供两件事，尽量复用生产代码路径（workspace._render_workspace → _render_step
→ 各面板函数），而不是另写一套渲染：

- `synth_entry(...)`: 造一条内存态项目条目（含合法 draft），配合对
  `webui.projects._load_projects/_save_projects` 的打桩使用，绝不落盘、不污染仓库。
- `open_workspace(...)`: 用一条合成项目 + 目标步骤去跑 Main.py，返回已 run() 的 AppTest。
- `goto_step(app, step)`: 在同一个 AppTest 会话里切到某步（模拟点"下一步"，但走会话
  缓存的 params，跨步状态不丢）。
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from streamlit.testing.v1 import AppTest
from unittest import mock

from app.models.schema import VideoParams

ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"

# 与 webui/workspace.py 的步骤 id 保持一致
CONTENT = "content"
VISUAL = "visual"
AUDIO = "audio"
OUTPUT = "output"

WS_PID = "p_ws_test"


def synth_entry(mode: str = "normal", **draft_kw) -> dict:
    """构造一条内存态项目条目。draft_kw 覆盖 VideoParams 字段。"""
    subject = draft_kw.pop("video_subject", "AI 让生活更美好")
    params = VideoParams(video_subject=subject)
    for key, value in draft_kw.items():
        if hasattr(params, key):
            setattr(params, key, value)
    if mode == "biography":
        params.video_source = "historical_archive"
    return {
        "id": WS_PID,
        "task_id": "",
        "title": "ws test",
        "mode": mode,
        "person": "",
        "language": "",
        "source": params.video_source,
        "status": "draft",
        "step": CONTENT,
        "created_at": 0,
        "updated_at": 0,
        "draft": params.model_dump(mode="json"),
    }


def project_patchers(entry: dict):
    """返回打桩 projects 读写，使工作区用合成条目、且提交时不落盘。"""
    from webui import projects

    return (
        mock.patch.object(projects, "_load_projects", return_value=[entry]),
        mock.patch.object(projects, "_save_projects", return_value=None),
    )


@contextmanager
def projects_patched(entry: dict):
    """单个上下文管理器形式打桩 projects 读写，便于塞进测试已有的 with 元组。"""
    p1, p2 = project_patchers(entry)
    with p1, p2:
        yield entry


def open_workspace(
    step: str = CONTENT,
    *,
    lang: str = "en",
    session: dict | None = None,
    default_timeout: int = 60,
) -> AppTest:
    """创建一个已 run() 的工作区 AppTest，面板渲染在指定步骤。

    生效的项目条目来自调用方对 `projects._load_projects` 的打桩（见 `project_patchers`），
    因此 mode/draft 都由那条条目决定；本函数只负责把会话路由到该项目并定位到 `step`。

    注意：调用方需保持 projects 打桩处于激活状态（与 config 打桩同一个 with 作用域），
    否则后续 widget 交互 rerun 时取不到合成条目、会退回项目列表。
    """
    app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=default_timeout)
    app.session_state["ui_language"] = lang
    app.session_state["ws_project_id"] = WS_PID
    app.session_state["ws_step"] = step
    for key, value in (session or {}).items():
        app.session_state[key] = value
    app.run()
    return app


def goto_step(app: AppTest, step: str, *, lang: str | None = None) -> AppTest:
    """在同一会话里切到某步：设置 ws_step 后 rerun（params 由会话缓存累积）。"""
    app.session_state["ws_step"] = step
    if lang is not None:
        app.session_state["ui_language"] = lang
    app.run()
    return app
