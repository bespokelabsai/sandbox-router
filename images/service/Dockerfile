# Jobs-only hosted-jobs service. Build from the repository root:
#
#   docker build -f images/service/Dockerfile -t sandbox-jobs-service .
#
# The image contains the package and a web server but no application: the
# deployer adds a host module (see examples/jobs_service_host.py) and names it
# with APP_MODULE or as the container argument.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY pyproject.toml README.md LICENSE /build/
COPY src /build/src
RUN pip install "/build[server,postgres,gcp-batch]" \
    && rm -rf /build \
    && groupadd --system --gid 10001 service \
    && useradd --system --uid 10001 --gid service --home-dir /app \
        --create-home --shell /usr/sbin/nologin service

# Host modules placed in /app are importable: uvicorn adds the working
# directory to the import path.
WORKDIR /app
USER 10001:10001

ENV PORT=8080
EXPOSE 8080

# One process per container; scale by adding instances. The first argument,
# when given, overrides APP_MODULE.
ENTRYPOINT ["sh", "-c", "exec uvicorn \"${1:-${APP_MODULE:?set APP_MODULE or pass module:attribute}}\" --host 0.0.0.0 --port \"${PORT:-8080}\"", "serve"]
