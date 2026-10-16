"""共享测试固件。

关键职责:保护开发机的 ``config.toml``。

背景(真实缺陷):WebUI 的 AppTest 测试会执行真实的 ``_save_runtime_config()``,
而该函数最终调用 ``config.save_config()`` 把进程级配置写回仓库根目录的
``config.toml``。结果是**跑一次 pytest 就会用测试里的临时音色/设置覆盖开发者的
真实配置**(现场出现 [ui] voice_name 被改成测试用的 en-AU 音色,导致随后所有
真实生成任务在音频阶段失败)。这里在会话开始时备份 config.toml,结束时原子恢复,
让测试永不污染仓库配置。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
_CONFIG = _ROOT / "config.toml"
_BACKUP = _ROOT / ".pytest_config.toml.bak"

# 与 app/services/voice.py 的免费 Edge TTS 音色保持一致:不依赖任何 Key,
# `_voice_preflight_error` 的付费 provider 探测不会误伤。
_DEFAULT_TEST_VOICE = "zh-CN-XiaoxiaoNeural"


@pytest.fixture(autouse=True)
def _ensure_default_voice_name(monkeypatch):
    """向 config.ui/config.app 注入可用默认音色,让流水线测试不依赖环境 config.toml。

    CI 红根因(已复现):相当一部分测试构造 VideoParams 时不传 voice_name,
    依赖 task._normalize_voice_params 从 [ui]/[app] 回填。开发机 config.toml
    有真实音色所以绿;干净 CI checkout 没有 config.toml,两处回填源都为空,
    任务在 preflight 就被 "voice_name is empty" 拦下,失败发生在被断言的阶段
    之前,failed_stage 断言全部落空。测试侧注入固定默认音色后,两个环境行为
    一致,且 monkeypatch 每例自动还原,不会污染其它用例对配置的自定义。
    """
    from app.config import config as app_config

    monkeypatch.setitem(app_config.ui, "voice_name", _DEFAULT_TEST_VOICE)
    monkeypatch.setitem(app_config.app, "voice_name", _DEFAULT_TEST_VOICE)
    yield


@pytest.fixture(scope="session", autouse=True)
def _protect_config_toml():
    """会话级:备份并在测试结束后恢复 config.toml。"""
    existed = _CONFIG.is_file()
    if existed:
        shutil.copy2(_CONFIG, _BACKUP)
    try:
        yield
    finally:
        if existed and _BACKUP.is_file():
            try:
                shutil.copy2(_BACKUP, _CONFIG)
            finally:
                try:
                    os.unlink(_BACKUP)
                except OSError:
                    pass
        elif not existed and _CONFIG.is_file():
            # 测试前不存在、测试中新建:清理掉,保持工作区干净。
            try:
                os.unlink(_CONFIG)
            except OSError:
                pass


# ── 测试护栏(收尾 #3):真实 DB / storage 全程隔离且零改动 ──────────────────
#
# 背景:相当一部分服务(person_repo / person_store / library / project_store /
# article_repo)默认落在开发机真实 storage 目录。哪怕只有一个测试忘了打临时
# env,也会把测试数据(person='Coffee'、task-123、空人物草稿……)写进真实的
# mpt.sqlite / personal.sqlite / projects.json,现场已多次因此污染开发数据。
#
# 做法:会话开始时把 storage 根与四个 DB/正文目录 env 全部重定向到临时目录
# (app.utils.storage_dir 现已识别 MPT_STORAGE_DIR),使任何“默认解析”都落到
# tmp;并在 yield 前后对真实文件做快照——mpt.sqlite 逐表行数、其余文件
# SHA-256。只要有任何代码绕过隔离写到真实文件,teardown 就 fail 并列出差异,
# 而不会静默地把污染留给开发者。
_REAL_STORAGE = _ROOT / "storage"
_REAL_DB_FILES = {
    "mpt.sqlite": _REAL_STORAGE / "library" / "mpt.sqlite",
    "personal.sqlite": _REAL_STORAGE / "library" / "personal.sqlite",
    "library.sqlite": _REAL_STORAGE / "library" / "library.sqlite",
    "projects.json": _REAL_STORAGE / "projects" / "projects.json",
}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _mpt_table_rows(db_file: Path) -> dict[str, int]:
    """只读连接统计 mpt.sqlite 各表行数;库不存在或不可读返回空。"""
    if not db_file.is_file():
        return {}
    try:
        conn = sqlite3.connect(f"file:{db_file.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        rows: dict[str, int] = {}
        for t in tables:
            try:
                rows[t] = int(conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])
            except sqlite3.Error:
                rows[t] = -1
        return rows
    finally:
        conn.close()


def _snapshot_real_state() -> dict:
    """对真实开发数据做一次指纹:文件存在标志 + SHA-256 + mpt 逐表行数。"""
    snap: dict[str, object] = {"sha": {}, "rows": {}}
    for name, path in _REAL_DB_FILES.items():
        if path.is_file():
            snap["sha"][name] = _sha256(path)
    snap["rows"] = _mpt_table_rows(_REAL_DB_FILES["mpt.sqlite"])
    return snap


# 重定向必须在 conftest 导入时(会话测试开始前)就生效,而不是等到 fixture 启动:
# app/asgi.py 在模块导入时就执行 `task_dir = utils.task_dir()` 并 mount StaticFiles,
# 若 env 晚于该导入设置,会出现“静态挂载指向真实 storage、测试写入 tmp”导致 404。
# 因此这里在模块顶层建立 tmp 并写入 env;session fixture 只负责前后快照与还原。
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="mpt-test-storage-"))
_TMP_STORAGE = _TMP_ROOT / "storage"
for _sub in ("library", "projects", "articles"):
    (_TMP_STORAGE / _sub).mkdir(parents=True, exist_ok=True)

_REDIRECTS = {
    "MPT_STORAGE_DIR": str(_TMP_STORAGE),
    "MPT_MAIN_DB": str(_TMP_STORAGE / "library" / "mpt.sqlite"),
    "MPT_PERSONAL_DB": str(_TMP_STORAGE / "library" / "personal.sqlite"),
    "MPT_LIBRARY_DB": str(_TMP_STORAGE / "library" / "library.sqlite"),
    "MPT_ARTICLES_DIR": str(_TMP_STORAGE / "articles"),
}
_SAVED_ENV = {k: os.environ.get(k) for k in _REDIRECTS}
os.environ.update(_REDIRECTS)


@pytest.fixture(scope="session", autouse=True)
def _isolate_storage_and_guard_real_data():
    """会话级:断言真实开发数据在整轮测试中零改动(env 重定向已在模块顶层完成)。"""
    before = _snapshot_real_state()
    try:
        yield
    finally:
        # 先恢复 env,再比对真实文件,确保比对不受临时值影响。
        for k, v in _SAVED_ENV.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        after = _snapshot_real_state()
        try:
            shutil.rmtree(_TMP_ROOT, ignore_errors=True)
        except OSError:
            pass

        problems: list[str] = []
        if before["sha"] != after["sha"]:
            for name in sorted(set(before["sha"]) | set(after["sha"])):
                b, a = before["sha"].get(name), after["sha"].get(name)
                if b != a:
                    problems.append(
                        f"{name}: SHA-256 {b} -> {a} (真实文件被测试写入)"
                    )
        b_rows, a_rows = before["rows"], after["rows"]
        for t in sorted(set(b_rows) | set(a_rows)):
            if b_rows.get(t) != a_rows.get(t):
                problems.append(
                    f"mpt.sqlite table '{t}': rows {b_rows.get(t)} -> {a_rows.get(t)}"
                )
        if problems:
            pytest.fail(
                "测试护栏(收尾#3)失败:以下真实开发数据在测试期间被改动,\n"
                "说明有代码没走临时目录,请定位并改为合成数据/临时路径:\n"
                + "\n".join(problems)
            )