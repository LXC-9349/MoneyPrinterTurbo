# Use an official Python runtime as a parent image
FROM python:3.11-slim-bullseye

# Set the working directory in the container
WORKDIR /MoneyPrinterTurbo

# 设置/MoneyPrinterTurbo目录权限为777
RUN chmod 777 /MoneyPrinterTurbo

ENV PYTHONPATH="/MoneyPrinterTurbo"

# 本地用户默认继续优先使用国内镜像；GitHub Actions 发布 GHCR 镜像时使用 default，
# 避免海外 runner 访问国内镜像过慢导致镜像发布长时间卡住。
ARG DOCKER_BUILD_MIRROR=china

# 系统依赖安装需要同时满足两点：国内环境保留镜像回退能力，所有镜像均
# 失败时必须让 Docker 构建立刻失败。旧循环最后执行的 sleep 总会返回 0，
# 导致 git/ffmpeg 未安装时仍生成不可用镜像。这里把“写入软件源”“安装”
# 和“三次重试”拆成边界清晰的 shell 函数，并用函数返回值决定是否继续。
# 所有软件源统一使用 HTTPS，避免部分网络环境直接拦截明文 HTTP 请求。
RUN set -u; \
    write_debian_sources() { \
        main_url="$1"; \
        security_url="$2"; \
        printf 'deb %s bullseye main\ndeb %s bullseye-updates main\ndeb %s bullseye-security main\n' \
            "$main_url" "$main_url" "$security_url" > /etc/apt/sources.list; \
        rm -rf /var/lib/apt/lists/*; \
    }; \
    install_system_dependencies() { \
        apt-get update && \
        apt-get install -y --no-install-recommends git ffmpeg; \
    }; \
    retry_system_dependencies() { \
        attempt=1; \
        while [ "$attempt" -le 3 ]; do \
            echo "Attempt $attempt: installing system dependencies"; \
            if install_system_dependencies; then \
                return 0; \
            fi; \
            echo "Attempt $attempt failed" >&2; \
            if [ "$attempt" -lt 3 ]; then \
                echo "Retrying in 5 seconds..." >&2; \
                sleep 5; \
            fi; \
            attempt=$((attempt + 1)); \
        done; \
        return 1; \
    }; \
    if [ "$DOCKER_BUILD_MIRROR" = "china" ]; then \
        write_debian_sources \
            "https://mirrors.aliyun.com/debian" \
            "https://mirrors.aliyun.com/debian-security"; \
        if ! retry_system_dependencies; then \
            echo "Aliyun mirror failed, switching to Tsinghua mirror" >&2; \
            write_debian_sources \
                "https://mirrors.tuna.tsinghua.edu.cn/debian" \
                "https://mirrors.tuna.tsinghua.edu.cn/debian-security"; \
            if ! install_system_dependencies; then \
                echo "Tsinghua mirror failed, switching to default Debian mirror" >&2; \
                write_debian_sources \
                    "https://deb.debian.org/debian" \
                    "https://deb.debian.org/debian-security"; \
                if ! install_system_dependencies; then \
                    echo "Failed to install system dependencies from all configured mirrors" >&2; \
                    exit 1; \
                fi; \
            fi; \
        fi; \
    else \
        echo "Using default Debian mirrors"; \
        write_debian_sources \
            "https://deb.debian.org/debian" \
            "https://deb.debian.org/debian-security"; \
        if ! retry_system_dependencies; then \
            echo "Failed to install system dependencies from the default Debian mirror" >&2; \
            exit 1; \
        fi; \
    fi; \
    rm -rf /var/lib/apt/lists/*

# 依赖单一事实来源：pyproject.toml + uv.lock（不再维护 requirements.txt）。
# 先只拷贝锁文件与项目定义以利用 Docker 层缓存，用 uv 精确复现锁定版本。
COPY pyproject.toml uv.lock ./

# 本地默认优先国内 PyPI 镜像；GHCR 发布使用官方 PyPI（DOCKER_BUILD_MIRROR=default）。
RUN pip install --no-cache-dir uv && \
    if [ "$DOCKER_BUILD_MIRROR" = "china" ]; then \
        export UV_DEFAULT_INDEX="https://mirrors.aliyun.com/pypi/simple/"; \
    else \
        export UV_DEFAULT_INDEX="https://pypi.org/simple"; \
    fi; \
    uv sync --frozen --no-dev --no-install-project

# Now copy the rest of the codebase into the image
COPY . .

# Expose the API port (config.listen_port，默认 8917)
EXPOSE 8917

# 默认命令启动 FastAPI 后端（SolidStart 前端为独立进程/静态产物，见 docs/QUICKSTART.md）。
# 容器内部监听 0.0.0.0，宿主机通过 docker 端口映射限制为 127.0.0.1。
CMD ["uv", "run", "--no-sync", "python", "main.py"]
