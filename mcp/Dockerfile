# Shared image for db-mcp and resume-mcp. Build context is the repo root.
# Compose selects the server via the `command` override.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY packages/core /app/packages/core
RUN pip install --no-cache-dir ./packages/core

COPY mcp/requirements.txt /app/mcp/requirements.txt
RUN pip install --no-cache-dir -r mcp/requirements.txt

COPY mcp /app/mcp

# Run from /app/mcp so `db_mcp` / `resume_mcp` are importable and `import mcp`
# resolves to the installed SDK (not this directory).
WORKDIR /app/mcp

# Overridden per service in docker-compose (db_mcp.server:app / resume_mcp.server:app).
CMD ["uvicorn", "db_mcp.server:app", "--host", "0.0.0.0", "--port", "8001"]
