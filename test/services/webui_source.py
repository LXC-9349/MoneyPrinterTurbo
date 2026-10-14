"""跨 webui 模块解析源码的测试辅助。

WebUI 原本是单个 ``webui/Main.py``;为确保可维护性已拆分为 ``webui/*.py``
(base/tasks/scripts/video/audio/subtitles/settings/generation 等),``Main.py``
仅保留薄入口。历史上很多测试用 ``ast.parse(Main.py)`` 再按名字挑选顶层
``FunctionDef``/``Assign`` 来"只编译待测纯函数"——拆分后这些定义不再位于
``Main.py``。这里提供等价的入口:把所有 webui 模块的顶层语句合并成一棵 AST,
使按名挑选依旧有效,无需每个测试各自知道符号搬到了哪个文件。
"""

from __future__ import annotations

import ast
from pathlib import Path

WEBUI_DIR = Path(__file__).resolve().parents[2] / "webui"


def module_paths() -> list[Path]:
    """返回 webui 目录下的全部模块路径(排序,便于复现)。"""
    return sorted(WEBUI_DIR.glob("*.py"))


def parse_module(name: str) -> ast.Module:
    """解析指定 webui 模块(如 ``"scripts.py"``)。"""
    return ast.parse((WEBUI_DIR / name).read_text(encoding="utf-8"))


def parse_all() -> ast.Module:
    """把所有 webui 模块的顶层语句合并为一棵 AST。

    测试只按名字挑选顶层定义,合并后等价于拆分前的单文件脚本。
    """
    body: list[ast.stmt] = []
    for path in module_paths():
        body.extend(ast.parse(path.read_text(encoding="utf-8")).body)
    return ast.fix_missing_locations(ast.Module(body=body, type_ignores=[]))


def find_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    """返回顶层指定名字的函数定义,找不到时抛 ``StopIteration``(与原测试一致)。"""
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def find_assignments(tree: ast.Module, names: set[str]) -> list[ast.stmt]:
    """返回目标名命中 ``names`` 的顶层赋值节点(含 ``AnnAssign``)。"""
    selected: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id in names for t in targets):
            selected.append(node)
    return selected