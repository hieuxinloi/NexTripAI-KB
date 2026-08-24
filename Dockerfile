FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8010

WORKDIR /app
RUN addgroup --system nextrip && adduser --system --ingroup nextrip nextrip
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY nextrip_graphrag ./nextrip_graphrag
USER nextrip
EXPOSE 8010
CMD ["sh", "-c", "uvicorn nextrip_graphrag.api.app:app --host 0.0.0.0 --port ${PORT}"]
