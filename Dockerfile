FROM python:3.11-slim

WORKDIR /app

# prometheus_client is a hard dependency (metrics.py always imports it);
# psycopg2-binary, redis, and the opentelemetry packages are optional at
# runtime (only used if DATABASE_URL / REDIS_URL / OTEL_EXPORTER_OTLP_ENDPOINT
# or AGENTHIVE_TRACING_CONSOLE are set) but installing them here means all
# of them are ready to use without rebuilding the image.
COPY requirements.txt .
RUN pip install --no-cache-dir \
    prometheus_client psycopg2-binary redis \
    opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp-proto-http

COPY . .

RUN useradd --create-home --shell /usr/sbin/nologin agenthive \
    && mkdir -p /data \
    && chown -R agenthive:agenthive /app /data
USER agenthive

ENV AGENTHIVE_HOST=0.0.0.0 \
    AGENTHIVE_PORT=8790 \
    AGENTHIVE_DB_PATH=/data/agenthive.db

EXPOSE 8790

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD python3 -c "\
import os, ssl, urllib.request; \
scheme = 'https' if os.environ.get('AGENTHIVE_TLS_CERT_FILE') else 'http'; \
ctx = ssl._create_unverified_context() if scheme == 'https' else None; \
import sys; \
sys.exit(0 if urllib.request.urlopen(f'{scheme}://127.0.0.1:8790/healthz', timeout=2, context=ctx).status == 200 else 1)"

CMD ["python3", "server.py"]
