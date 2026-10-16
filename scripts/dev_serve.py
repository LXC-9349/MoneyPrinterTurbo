"""开发期启动器：在当前进程注入前端 dev 源的 CORS 白名单，再以 uvicorn 热重载
方式启动 app.asgi:app。

热重载说明：uvicorn 的 reload 监视子进程由本进程 spawn，会继承这里设置的环境
变量（含 CORS_ALLOWED_ORIGINS）与 sys.path，因此字符串导入路径是安全的；早期
“子进程丢环境变量”的现象源于绕过本脚本直接以字符串路径启动。若需要旧的单进程
行为（例如调试断点），设置 MPT_DEV_RELOAD=0 即可关闭热重载。

仅用于本地开发验证，不改变生产启动方式（main.py）。
"""

import os
import sys

# 把仓库根目录加入模块搜索路径（本脚本位于 scripts/ 下），确保能导入 app 包。
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

# 开发期 CORS 注入：在导入 app.asgi（导入时读取该环境变量）之前，确保前端 dev 源
# 已在白名单里。若调用方已通过真实环境变量传入则沿用，否则回退到本地 dev 默认值。
# 仅本地开发验证使用，生产启动（main.py）不受影响。
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:7310")

import uvicorn  # noqa: E402

from app.config import config  # noqa: E402

# 默认开启热重载：app/ 下任一 .py 变更（含新增）自动重启 worker，改码后无需
# 手动重启。watchfiles 缺失时 uvicorn 会退化为轮询监视，仍可用但略有延迟。
RELOAD = os.environ.get("MPT_DEV_RELOAD", "1").strip().lower() not in (
    "0",
    "false",
    "off",
)


def _run_kwargs():
    return dict(
        host=config.listen_host,
        port=config.listen_port,
        log_level="warning",
    )


if __name__ == "__main__":
    if RELOAD:
        uvicorn.run(
            "app.asgi:app",
            **_run_kwargs(),
            reload=True,
            reload_dirs=[os.path.join(REPO_ROOT, "app")],
            reload_delay=0.5,
        )
    else:
        from app.asgi import app  # noqa: E402

        uvicorn.run(app, **_run_kwargs())
