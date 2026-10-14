"""开发期启动器：在当前进程内导入 app.asgi（导入时读取 CORS_ALLOWED_ORIGINS），
再把已构建好的 app 对象直接交给 uvicorn，避免字符串导入路径派生子进程导致
注入的环境变量丢失。仅用于本地开发验证，不改变生产启动方式（main.py）。
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

from app.asgi import app  # noqa: E402
from app.config import config  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(
        app,
        host=config.listen_host,
        port=config.listen_port,
        log_level="warning",
    )
