FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH=/root/.local/bin:${PATH}

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git openssh-client procps systemd tini \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 aiagent \
    && useradd --uid 10001 --gid aiagent --create-home --shell /usr/sbin/nologin aiagent

COPY pyproject.toml README.md ./
COPY config ./config
COPY dashboard ./dashboard
COPY shared ./shared
COPY watcher ./watcher
COPY worker ./worker
COPY vitastor ./vitastor
COPY scripts ./scripts

RUN pip install --upgrade pip \
    && pip install .

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "dashboard.app:app", "--host", "0.0.0.0", "--port", "8000"]
