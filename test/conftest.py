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

import os
import shutil
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