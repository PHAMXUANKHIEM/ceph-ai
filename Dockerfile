FROM docker.io/library/python:3.11-slim@sha256:da047cb8f9d1d98e5c070f5300ba9f7274e33b8fc0e5be5ed88740aed1b95ba9

ARG GIT_COMMIT=unknown
LABEL org.opencontainers.image.revision=$GIT_COMMIT

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH=/root/.local/bin:${PATH}

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates=20250419 \
       curl=8.14.1-2+deb13u5 \
       git=1:2.47.3-0+deb13u1 \
       openssh-client=1:10.0p1-7+deb13u4 \
       procps=2:4.0.4-9 \
       systemd=257.13-1~deb13u1 \
       tini=0.19.0-3+b8 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 aiagent \
    && useradd --uid 10001 --gid aiagent --create-home --shell /usr/sbin/nologin aiagent

COPY pyproject.toml README.md requirements-prod.lock ./
COPY config ./config
COPY dashboard ./dashboard
COPY shared ./shared
COPY watcher ./watcher
COPY worker ./worker
COPY vitastor ./vitastor
COPY scripts ./scripts
COPY alembic.ini ./
COPY alembic ./alembic
COPY docs/ceph-ai-rca-knowledge.md docs/runbook-dr.md docs/runbook-log-intelligence.md docs/crush-map-monitor.md ./docs/

# Runtime dependencies are pinned transitively, with wheel hashes. Do not let
# installing the local package resolve a second, newer dependency set.
RUN python -m pip install --disable-pip-version-check --require-hashes -r requirements-prod.lock \
    && python -m pip install --disable-pip-version-check --no-deps --no-build-isolation . \
    && python -m pip uninstall --yes setuptools wheel \
    && test -s dashboard/static/ceph-health/app.js \
    && test -s dashboard/static/ceph-health/style.css

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "dashboard.app:app", "--host", "0.0.0.0", "--port", "8000"]
