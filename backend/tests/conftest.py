"""Phase 1 test fixtures: spawn db-mcp + resume-mcp over HTTP against a local
storage dir, so the isolation/erasure/round-trip tests exercise the real
streamable-HTTP boundary. Skips cleanly when the DB is unreachable.
"""

from __future__ import annotations

import os
import pathlib
import socket
import subprocess
import sys
import time

import pytest

# Deterministic secrets for the test boundary (env wins over .env).
os.environ.setdefault("JWT_SECRET", "test-mcp-secret")
os.environ.setdefault("MCP_SHARED_SECRET", "test-mcp-secret")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://jobpilot:jobpilot_dev_pw@localhost:5432/jobpilot",
)

REPO = pathlib.Path(__file__).resolve().parents[2]
MCP_DIR = REPO / "mcp"


@pytest.fixture(autouse=True)
def _reset_async_engine():
    """Each pytest-asyncio test gets a fresh event loop; rebuild the cached async
    engine per test so asyncpg connections never outlive their loop."""
    import jobpilot_core.db as _db

    _db._engine = None
    _db._sessionmaker = None
    yield
    _db._engine = None
    _db._sessionmaker = None

DB_MCP_PORT = 8101
RESUME_MCP_PORT = 8102


def _db_host_port() -> tuple[str, int]:
    url = os.environ["DATABASE_URL"]
    hostpart = url.split("@", 1)[-1].split("/", 1)[0]
    host, _, port = hostpart.partition(":")
    return host, int(port or "5432")


def _db_reachable() -> bool:
    host, port = _db_host_port()
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def _wait_port(port: int, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.3)
    raise RuntimeError(f"MCP service on port {port} did not start")


@pytest.fixture(scope="session")
def mcp_services(tmp_path_factory):
    if not _db_reachable():
        pytest.skip("database not reachable (set DATABASE_URL / start Postgres)")

    from alembic import command
    from alembic.config import Config

    storage_dir = tmp_path_factory.mktemp("storage")
    env = {
        **os.environ,
        "STORAGE_BACKEND": "local",
        "STORAGE_LOCAL_DIR": str(storage_dir),
    }

    # Ensure schema is present (idempotent). Pin script_location absolutely since
    # pytest's cwd differs from the repo root.
    cfg = Config(str(REPO / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "packages" / "core" / "migrations"))
    command.upgrade(cfg, "head")

    procs = []
    for module, port in (("db_mcp.server", DB_MCP_PORT), ("resume_mcp.server", RESUME_MCP_PORT)):
        procs.append(
            subprocess.Popen(
                [sys.executable, "-m", "uvicorn", f"{module}:app", "--port", str(port),
                 "--log-level", "warning"],
                cwd=str(MCP_DIR),
                env=env,
            )
        )
    try:
        _wait_port(DB_MCP_PORT)
        _wait_port(RESUME_MCP_PORT)
        time.sleep(1.0)  # let the session managers finish starting
        yield {
            "db": f"http://127.0.0.1:{DB_MCP_PORT}/mcp",
            "resume": f"http://127.0.0.1:{RESUME_MCP_PORT}/mcp",
            "storage_dir": storage_dir,
        }
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
