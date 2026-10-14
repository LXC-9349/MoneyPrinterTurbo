# 开发期启动 FastAPI 后端：注入前端 dev 源到 CORS 白名单，使浏览器可直连写/轮询。
# 仅本地开发用（等价于计划中的“开发期 CORS 注入”）。
# 需从仓库根目录调用（uv 依赖当前目录发现项目/venv）。
$env:CORS_ALLOWED_ORIGINS = "http://localhost:7310"
Set-Location -Path "E:\pj-vd\MoneyPrinterTurbo"
uv run python scripts\dev_serve.py
