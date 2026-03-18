from collections.abc import Generator
from contextlib import contextmanager

from psycopg import Connection
from psycopg import OperationalError as PsycopgOperationalError
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.core.config import get_settings
from app.core.errors import ApiError

_pool: ConnectionPool | None = None


def _recover_pool_connections() -> None:
    if _pool is None:
        return

    try:
        _pool.check()
    except Exception:
        # If the database is still unreachable, the original request should
        # still fail fast with a 503. This just gives the pool a chance to
        # discard broken idle connections for the next request.
        return


def init_db_pool() -> None:
    global _pool
    if _pool is not None:
        return

    settings = get_settings()
    _pool = ConnectionPool(
        conninfo=settings.database_url,
        min_size=1,
        max_size=10,
        timeout=settings.db_pool_acquire_timeout_sec,
        kwargs={
            "row_factory": dict_row,
            "connect_timeout": settings.db_connect_timeout_sec,
            # Supabase pooler (PgBouncer transaction mode) is incompatible with
            # psycopg prepared statement lifecycle across pooled connections.
            "prepare_threshold": None,
        },
        check=ConnectionPool.check_connection,
    )


def close_db_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def get_db_conn() -> Generator[Connection, None, None]:
    if _pool is None:
        init_db_pool()

    if _pool is None:
        raise ApiError(status_code=500, code="db_init_failed", message="Database pool is not initialized.")

    try:
        with _pool.connection() as conn:
            yield conn
    except PsycopgOperationalError as exc:
        _recover_pool_connections()
        raise ApiError(
            status_code=503,
            code="db_unreachable",
            message=(
                "Database connection is temporarily unavailable. The pool is "
                "recovering a dropped connection; retry shortly."
            ),
        ) from exc


@contextmanager
def get_pooled_conn() -> Generator[Connection, None, None]:
    if _pool is None:
        init_db_pool()
    if _pool is None:
        raise RuntimeError("Database pool is not initialized.")

    try:
        with _pool.connection() as conn:
            yield conn
    except PsycopgOperationalError:
        _recover_pool_connections()
        raise


def apply_db_auth_context(conn: Connection, user_id: str) -> None:
    """
    Set Supabase-compatible JWT claim context for this DB transaction so auth.uid()
    and profile-scoped RPC checks work when using direct Postgres connections.
    """
    with conn.cursor() as cur:
        cur.execute("select set_config('request.jwt.claim.sub', %s, true)", (user_id,))
        cur.execute("select set_config('request.jwt.claim.role', 'authenticated', true)")
