FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    NEXTRIP_KB_ROOT=/app \
    PORT=8010

WORKDIR /app

RUN useradd --uid 50000 --gid 0 --create-home --home-dir /home/nextrip nextrip

COPY deploy/traffic/requirements.txt /tmp/traffic-requirements.txt
RUN pip install --no-cache-dir -r /tmp/traffic-requirements.txt

COPY nextrip_pipeline ./nextrip_pipeline
COPY nextrip_traffic ./nextrip_traffic
COPY config ./config

RUN mkdir -p /app/data/current/place /app/data/current/traffic \
    && chown -R 50000:0 /app \
    && chmod -R g=u /app

USER 50000:0

EXPOSE 8010

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/ready', timeout=3).read()" || exit 1

CMD ["python", "-m", "uvicorn", "nextrip_traffic.api:app", "--host", "0.0.0.0", "--port", "8010", "--workers", "2"]
