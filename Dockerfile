# The ReCET web app (example.org/ReCET). Builds on arm64 (the EC2 t4g box) and amd64.
#
#   docker compose up -d --build                     # see docker-compose.yml for volumes and settings
#   docker compose exec recet recet-web create-admin you@example.com
#
# The reference data (electric ACC parquets, CEDARS impact-profile library) is NOT in the image: it is mounted read-only at /data.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    RECET_HOME=/var/lib/recet \
    RECET_DATA=/data \
    MPLBACKEND=Agg

RUN useradd --create-home --uid 10001 recet \
 && mkdir -p /var/lib/recet /data \
 && chown recet:recet /var/lib/recet

WORKDIR /app
# the locked versions first (requirements/README.md), every file checked against its hash; then ReCET itself, which adds no dependency
COPY requirements/web.txt requirements/web.txt
RUN pip install --require-hashes -r requirements/web.txt
COPY pyproject.toml README.md ./
COPY recet ./recet
RUN pip install --no-deps . && rm -rf /app/build

USER recet
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3000/healthz', timeout=4).status == 200 else 1)"

CMD ["recet-web", "serve", "--host", "0.0.0.0", "--port", "3000"]
