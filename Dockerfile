# Build context is option-2-adk/:
#   docker build -f Dockerfile .
#
# One image, one Cloud Run service. All five ADK agents, the orchestration
# engine and the dashboard run in this process.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY adk_csuite/ ./adk_csuite/
COPY agents/ ./agents/
COPY context/ ./context/

RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

ENV PORT=8080
EXPOSE 8080

CMD exec uvicorn adk_csuite.main:app --host 0.0.0.0 --port ${PORT}
