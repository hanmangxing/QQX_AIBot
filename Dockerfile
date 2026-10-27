# QQ星 QQX_AIBot —— 一站部署式 QQ AI 伴侣
#
# 这个镜像只打包控制台本体（Python + 依赖 + 源码）。
# QQ 协议端（NapCat / SnowLuma）是独立程序、要扫码登录 QQ，不放进本镜像，
# 另起一个容器或跑在宿主机上，在 WebUI 里填它的 WebSocket 地址即可。

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

# 时区：容器默认 UTC，QQ 伴侣的时间感知要靠本地时间，必须装 tzdata
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends tzdata; \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime; \
    echo $TZ > /etc/timezone; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖单独一层：只改代码时不用重装
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8090/',timeout=4)" || exit 1

CMD ["python", "start.py"]
