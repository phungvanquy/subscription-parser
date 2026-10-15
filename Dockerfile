# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.11-slim-bookworm@sha256:0a310eeecf4e1f5a0743f9a6520c90c88d089c903ca5fd283f501e3a805f5f89
FROM ${PYTHON_IMAGE} AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

FROM base AS cores
COPY scripts/install_client_cores.py /build/install_client_cores.py
RUN python /build/install_client_cores.py --destination /opt/client-cores \
    && chmod -R a+rX /opt/client-cores

FROM base AS application
RUN groupadd --gid 10001 parser \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /data parser \
    && mkdir -p /data /app/scripts \
    && chown -R 10001:10001 /data /app
COPY --from=cores /opt/client-cores /opt/client-cores
COPY --chown=10001:10001 subscription_parser ./subscription_parser
COPY --chown=10001:10001 scripts/import_state.py ./scripts/import_state.py
COPY --chown=10001:10001 .env.example ./.env.example
ENV PARSER_DATA_DIR=/data \
    PARSER_TOOLS_DIR=/opt/client-cores
USER parser
EXPOSE 8099
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8099/healthz', timeout=3).read()"]
CMD ["python", "-m", "subscription_parser.app", "--host", "0.0.0.0", "--port", "8099"]

FROM application AS development
USER root
COPY requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY --chown=10001:10001 tests ./tests
COPY --chown=10001:10001 scripts/browser_smoke.py ./scripts/browser_smoke.py
COPY --chown=10001:10001 scripts/connectivity_fixture.py ./scripts/connectivity_fixture.py
USER parser

FROM application AS production
