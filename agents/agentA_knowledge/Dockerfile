# Build context is ./agents so the shared/ package is available.
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ ./shared/
COPY agentA_knowledge/ ./

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8001/health')"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001"]
