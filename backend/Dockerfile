# Build context is the repo root (so packages/core is available).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install the shared core package first (own layer for caching).
COPY packages/core /app/packages/core
RUN pip install --no-cache-dir ./packages/core

# Backend-specific deps.
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# App code + migration config (alembic runs from /app).
COPY backend /app/backend
COPY alembic.ini /app/alembic.ini

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://localhost:8000/health').status==200 else sys.exit(1)"

# Default: run the API. The `migrate` service overrides this command.
CMD ["sh", "-c", "cd /app/backend && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
