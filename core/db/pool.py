"""
PostgreSQL database connection pool manager using psycopg_pool.
"""

from __future__ import annotations

import structlog
from psycopg_pool import ConnectionPool, AsyncConnectionPool
from config import settings

log = structlog.get_logger(__name__)

sync_pool: ConnectionPool | None = None
async_pool: AsyncConnectionPool | None = None


def init_pools() -> None:
    """Initialize both synchronous and asynchronous database connection pools."""
    global sync_pool, async_pool

    if not settings.postgres_host:
        log.warning("db_pool_init_skipped", reason="postgres_host not configured")
        return

    conn_params = settings.postgres_conn_params()
    # Remove autocommit since connection parameters shouldn't enforce it for the pool itself
    conn_params.pop("autocommit", None)

    host = conn_params["host"]
    port = conn_params["port"]
    dbname = conn_params["dbname"]
    user = conn_params["user"]
    password = conn_params["password"]

    conn_str = f"host={host} port={port} dbname={dbname} user={user} password={password}"

    if sync_pool is None:
        try:
            sync_pool = ConnectionPool(
                conninfo=conn_str,
                min_size=2,
                max_size=10,
                open=True,
            )
            log.info("sync_db_pool_initialized", host=host, port=port, dbname=dbname)
        except Exception as e:
            log.error("sync_db_pool_init_failed", error=str(e))

    if async_pool is None:
        try:
            async_pool = AsyncConnectionPool(
                conninfo=conn_str,
                min_size=2,
                max_size=10,
                open=False,
            )
            log.info("async_db_pool_created", host=host, port=port, dbname=dbname)
        except Exception as e:
            log.error("async_db_pool_creation_failed", error=str(e))


async def open_async_pool() -> None:
    """Open the async database pool."""
    global async_pool
    if async_pool is not None:
        try:
            await async_pool.open()
            log.info("async_db_pool_opened")
        except Exception as e:
            log.error("async_db_pool_open_failed", error=str(e))


async def close_pools() -> None:
    """Gracefully close sync and async connection pools."""
    global sync_pool, async_pool

    if async_pool is not None:
        try:
            await async_pool.close()
            log.info("async_db_pool_closed")
        except Exception as e:
            log.error("async_db_pool_close_failed", error=str(e))
        async_pool = None

    if sync_pool is not None:
        try:
            sync_pool.close()
            log.info("sync_db_pool_closed")
        except Exception as e:
            log.error("sync_db_pool_close_failed", error=str(e))
        sync_pool = None


def get_sync_pool() -> ConnectionPool:
    """Get the active synchronous connection pool."""
    global sync_pool
    if sync_pool is None:
        init_pools()
    if sync_pool is None:
        raise RuntimeError("Synchronous database pool is not initialized")
    return sync_pool


def get_async_pool() -> AsyncConnectionPool:
    """Get the active asynchronous connection pool."""
    global async_pool
    if async_pool is None:
        init_pools()
    if async_pool is None:
        raise RuntimeError("Asynchronous database pool is not initialized")
    return async_pool


class ConnectionProxy:
    """
    A connection proxy that mimics a psycopg connection context manager
    but acquires and releases the connection from a psycopg connection pool.
    """
    def __init__(self, pool: ConnectionPool):
        self.pool = pool
        self.conn_ctx = pool.connection()
        self.conn = self.conn_ctx.__enter__()

    def __enter__(self):
        return self.conn.__enter__()

    def __exit__(self, exc_type, exc_val, exc_tb):
        return self.conn.__exit__(exc_type, exc_val, exc_tb)

    def cursor(self, *args, **kwargs):
        return self.conn.cursor(*args, **kwargs)

    def commit(self):
        return self.conn.commit()

    def rollback(self):
        return self.conn.rollback()

    def close(self):
        try:
            self.conn_ctx.__exit__(None, None, None)
        except Exception:
            pass

