# BA-AD 需要 glibc 2.38 及以上，使用 Debian trixie 避免旧版系统不兼容。
FROM python:3.12-slim-trixie

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY scripts/ ./scripts/
COPY config/ ./config/

EXPOSE 18888
ENTRYPOINT ["python", "scripts/serve_resources.py"]
CMD ["--host", "0.0.0.0"]
