# jobbot: the always-on bot and its web app.
#   docker compose up -d        (see docker-compose.yml)
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY jobbot ./jobbot
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 1000 jobbot \
    && mkdir /data && chown jobbot /data

USER jobbot
ENV JOBBOT_DB=/data/jobbot.db \
    JOBBOT_HOST=0.0.0.0 \
    JOBBOT_PORT=8080 \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"
CMD ["jobbot", "serve"]
